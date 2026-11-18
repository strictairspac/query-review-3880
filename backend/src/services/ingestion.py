"""迟到账单入库与补算批次编排。

- 入库在同一事务内锁定账期行，分配单调递增水位；
- 关账后的入库才视为迟到，立即让受影响范围内的证据失效并登记补算；
- 补算按 (evidence_id, target_seq) 幂等去重，重复投递不会重复计入；
- 补算结果采用乐观版本提交，与并发的新复核互不覆盖。
"""
from fastapi import HTTPException
from sqlalchemy import select, text

from ..models.backfill import Backfill
from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.ingestion import BillIngestion
from ..models.period import SettlementPeriod
from ..services.batch import BACKFILL_QUEUE_KEY, queue
from .watermark import lock_period, refresh_stability, watermark_info

INGEST_STATUSES = ('settled', 'pending')


def _duplicate(db, period, item):
    by_ref = db.scalar(
        select(BillIngestion).where(BillIngestion.period == period, BillIngestion.external_ref == item.external_ref)
    )
    if by_ref:
        return by_ref
    return db.scalar(select(BillIngestion).where(BillIngestion.invoice_id == item.invoice_id))


def _existing_invoice_period(db, invoice_id):
    return db.execute(
        text('SELECT settlement_period FROM invoices WHERE invoice_id = :invoice_id'), {'invoice_id': invoice_id}
    ).scalar()


def ensure_backfill(db, period_obj, evidence, target_seq, customers, reason=None):
    """幂等登记补算。目标水位不前进时复用已有记录。"""
    existing = db.scalar(
        select(Backfill).where(Backfill.evidence_id == evidence.id, Backfill.target_seq == target_seq)
    )
    if existing:
        if existing.status == 'done':
            return existing
        merged = sorted(set(existing.affected_customers or []) | set(customers))
        if merged != (existing.affected_customers or []):
            existing.affected_customers = merged
            if existing.status in ('queued', 'interrupted'):
                existing.status = 'queued'
                existing.error = None
                existing.reason = reason or existing.reason
        return existing
    backfill = Backfill(
        period=period_obj.period,
        batch_id=evidence.batch_id,
        evidence_id=evidence.id,
        target_seq=target_seq,
        affected_customers=sorted(set(customers)),
        status='queued',
        reason=reason or f'重算水位 {target_seq} 之前的迟到账单',
        attempts=0,
    )
    db.add(backfill)
    db.flush()
    return backfill


def enqueue_backfills(ids):
    for ident in ids or []:
        queue.rpush(BACKFILL_QUEUE_KEY, ident)


def _invalidate_evidence(db, period_obj, customers):
    """让该账期证据失效，并为每个证据登记补算。只重算受影响客户。

    证据的旧结果与未受影响客户行原样保留，补算完成前继续可查。
    """
    evidence_rows = db.scalars(
        select(Evidence)
        .where(Evidence.period == period_obj.period, Evidence.state.in_(('valid', 'stale')))
        .order_by(Evidence.id)
    ).all()
    created = []
    for evidence in evidence_rows:
        merged = sorted(set(evidence.affected_customers or []) | set(customers))
        backfill = ensure_backfill(
            db,
            period_obj,
            evidence,
            target_seq=period_obj.ingest_seq,
            customers=merged,
            reason=f'迟到账单入库至水位 {period_obj.ingest_seq}，重算受影响客户：{"、".join(merged)}',
        )
        evidence.state = 'stale'
        evidence.affected_customers = merged
        evidence.invalidated_seq = period_obj.ingest_seq
        evidence.reason = f'迟到账单入库（水位 {period_obj.ingest_seq}），受影响客户待补算：{"、".join(merged)}'
        # 版本前进：正在执行的补算若基于旧版本提交会被拒绝，避免覆盖最新失效范围。
        evidence.version = (evidence.version or 1) + 1
        batch = db.get(Batch, evidence.batch_id)
        if batch and batch.status in ('passed', 'different', 'waiting_watermark'):
            batch.status = 'backfilling'
            batch.status_reason = f'证据因迟到账单失效，补算受影响客户：{"、".join(merged)}'
        if backfill.id not in [x.id for x in created]:
            created.append(backfill)
    return created


def ingest_bills(db, period, items):
    """迟到账单入库；返回本次水位、受影响客户与补算批次。"""
    if not items:
        raise HTTPException(422, '至少提交一笔账单')
    for item in items:
        if item.status not in INGEST_STATUSES:
            raise HTTPException(422, f'账单状态只能是 {"/".join(INGEST_STATUSES)}')
        if item.amount < 0:
            raise HTTPException(422, '金额不能为负')
        if item.temperature < -80 or item.temperature > 80:
            raise HTTPException(422, '温度超出合理范围')

    period_obj = lock_period(db, period)
    late = period_obj.status == 'closed'

    created, skipped = [], []
    seen_refs, seen_invoices = set(), set()
    next_seq = period_obj.ingest_seq
    for item in items:
        if item.external_ref in seen_refs or item.invoice_id in seen_invoices:
            skipped.append({'external_ref': item.external_ref, 'invoice_id': item.invoice_id, 'reason': '请求内重复'})
            continue
        seen_refs.add(item.external_ref)
        seen_invoices.add(item.invoice_id)
        duplicate = _duplicate(db, period, item)
        if duplicate:
            skipped.append({'external_ref': item.external_ref, 'invoice_id': item.invoice_id, 'reason': '已入库，幂等跳过'})
            continue
        owner = _existing_invoice_period(db, item.invoice_id)
        if owner:
            raise HTTPException(409, f'invoice_id {item.invoice_id} 已归属账期 {owner}，不能重复入账')
        next_seq += 1
        created.append((next_seq, item))

    if not created:
        refresh_stability(db, period_obj)
        db.commit()
        return {
            'period': period,
            'created': 0,
            'skipped': skipped,
            'ingest_seq': period_obj.ingest_seq,
            'stable_seq': period_obj.stable_seq,
            'late': late,
            'affected_customers': [],
            'backfills': [],
            'watermark': watermark_info(db, period_obj),
        }

    for seq, item in created:
        db.add(
            BillIngestion(
                period=period,
                seq=seq,
                external_ref=item.external_ref,
                invoice_id=item.invoice_id,
                customer=item.customer,
                payload=item.model_dump(),
            )
        )
        db.execute(
            text(
                'INSERT INTO invoices (invoice_id,customer,route,amount,temperature,status,settlement_period,ingest_seq) '
                'VALUES (:invoice_id,:customer,:route,:amount,:temperature,:status,:period,:seq)'
            ),
            {
                'invoice_id': item.invoice_id,
                'customer': item.customer,
                'route': item.route,
                'amount': item.amount,
                'temperature': item.temperature,
                'status': item.status,
                'period': period,
                'seq': seq,
            },
        )
    period_obj.ingest_seq = created[-1][0]
    customers = sorted({item.customer for _, item in created})

    backfills = []
    if late:
        period_obj.auto_close = True
        backfills = _invalidate_evidence(db, period_obj, customers)
    created_elsewhere = refresh_stability(db, period_obj)
    queued = list(
        db.scalars(select(Backfill.id).where(Backfill.period == period, Backfill.status == 'queued'))
    )
    db.commit()
    enqueue_backfills(sorted(set(queued) | {b.id for b in backfills} | set(created_elsewhere)))
    return {
        'period': period,
        'created': len(created),
        'skipped': skipped,
        'ingest_seq': period_obj.ingest_seq,
        'stable_seq': period_obj.stable_seq,
        'late': late,
        'affected_customers': customers,
        'backfills': [
            {'id': b.id, 'evidence_id': b.evidence_id, 'batch_id': b.batch_id, 'target_seq': b.target_seq, 'status': b.status}
            for b in backfills
        ],
        'watermark': watermark_info(db, period_obj),
    }


def list_backfills(db, period=None):
    stmt = select(Backfill).order_by(Backfill.id.desc())
    if period:
        stmt = stmt.where(Backfill.period == period)
    return [serialize_backfill(db, x) for x in db.scalars(stmt).all()]


def serialize_backfill(db, x):
    return {
        'id': x.id,
        'period': x.period,
        'batch_id': x.batch_id,
        'evidence_id': x.evidence_id,
        'target_seq': x.target_seq,
        'affected_customers': x.affected_customers or [],
        'status': x.status,
        'attempts': x.attempts,
        'reason': x.reason,
        'error': x.error,
        'created_at': str(x.created_at),
        'updated_at': str(x.updated_at),
    }


def get_backfill(db, ident):
    obj = db.get(Backfill, ident)
    if not obj:
        raise HTTPException(404, '补算批次不存在')
    return serialize_backfill(db, obj)


def retry_backfill(db, ident):
    """人工恢复失败的补算：重置为排队并重新投递，结果仍按水位与版本幂等。"""
    obj = db.get(Backfill, ident)
    if not obj:
        raise HTTPException(404, '补算批次不存在')
    if obj.status not in ('failed', 'interrupted'):
        raise HTTPException(409, '只有失败或中断的补算需要人工重试')
    obj.status = 'queued'
    obj.attempts = 0
    obj.error = None
    period_obj = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == obj.period))
    if period_obj:
        from .watermark import refresh_stability

        refresh_stability(db, period_obj)
    db.commit()
    enqueue_backfills([obj.id])
    return serialize_backfill(db, obj)
