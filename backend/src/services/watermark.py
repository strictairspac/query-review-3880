"""账期入库水位：状态判定、稳定推进与批次状态收敛。

水位稳定的定义（缺一不可）：
1. 账期已关闭（open 时随时可能入库，不得判定稳定）；
2. 没有排队/执行中/中断待恢复的补算；
3. 没有失败的补算；
4. 每条非失败证据都采用了当前 ingest_seq；
5. stable_seq 已追平 ingest_seq。

水位不稳定时，等价结果只能停在 waiting_watermark，不得发布 passed。
"""
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, text

from ..constants.enums import BACKFILL_ACTIVE_STATUSES
from ..models.backfill import Backfill
from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.period import SettlementPeriod

PERIOD_PATTERN = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def validate_period_name(period):
    if not isinstance(period, str) or not PERIOD_PATTERN.match(period):
        raise ValueError('账期格式应为 YYYY-MM，例如 2026-10')
    return period


def insert_period_if_missing(db, period):
    db.execute(
        text(
            'INSERT INTO settlement_periods (period,status,ingest_seq,stable_seq,auto_close) '
            "VALUES (:period,'open',0,0,false) ON CONFLICT (period) DO NOTHING"
        ),
        {'period': period},
    )


def ensure_period(db, period):
    validate_period_name(period)
    insert_period_if_missing(db, period)
    return db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period))


def lock_period(db, period):
    """锁定账期行，串行化水位分配与稳定性推进。"""
    validate_period_name(period)
    insert_period_if_missing(db, period)
    return db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period).with_for_update())


def _count(db, model, *conditions):
    return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def watermark_info(db, period_obj):
    if period_obj is None:
        return {
            'period': None,
            'status': 'missing',
            'stable': False,
            'state': 'waiting',
            'label': '待等水位',
            'reason': '账期尚未建立水位记录',
            'ingest_seq': 0,
            'stable_seq': 0,
            'active_backfills': 0,
            'failed_backfills': 0,
            'incomplete_evidence': 0,
        }
    period = period_obj.period
    active = _count(
        db, Backfill, Backfill.period == period, Backfill.status.in_(BACKFILL_ACTIVE_STATUSES)
    )
    failed = _count(db, Backfill, Backfill.period == period, Backfill.status == 'failed')
    incomplete = _count(
        db,
        Evidence,
        Evidence.period == period,
        Evidence.state != 'failed',
        Evidence.watermark_seq < period_obj.ingest_seq,
    )
    stable = (
        period_obj.status == 'closed'
        and active == 0
        and failed == 0
        and incomplete == 0
        and period_obj.stable_seq == period_obj.ingest_seq
    )
    if stable:
        state, label, reason = 'stable', '完成', '账期已关闭，入库水位已稳定'
    elif active:
        state, label = 'backfilling', '补算'
        reason = f'迟到账单使 {active} 个补算批次排队或执行中，水位未稳定'
    elif failed:
        state, label = 'waiting', '待等水位'
        reason = f'{failed} 个补算批次失败，水位未稳定，需人工恢复'
    elif period_obj.status != 'closed':
        state, label = 'waiting', '待等水位'
        reason = '账期尚未关闭，迟到账单仍可能入库'
    elif incomplete:
        state, label = 'waiting', '待等水位'
        reason = f'{incomplete} 条证据尚未采用最新入库水位'
    else:
        state, label = 'waiting', '待等水位'
        reason = '入库水位尚未追平，等待补算完成'
    return {
        'period': period,
        'status': period_obj.status,
        'stable': stable,
        'state': state,
        'label': label,
        'reason': reason,
        'ingest_seq': period_obj.ingest_seq,
        'stable_seq': period_obj.stable_seq,
        'active_backfills': active,
        'failed_backfills': failed,
        'incomplete_evidence': incomplete,
    }


def serialize_period(db, period_obj):
    info = watermark_info(db, period_obj)
    info.update(
        {
            'id': period_obj.id,
            'closed_at': str(period_obj.closed_at) if period_obj.closed_at else None,
            'created_at': str(period_obj.created_at),
            'auto_close': bool(period_obj.auto_close),
            'evidence_count': _count(db, Evidence, Evidence.period == period_obj.period),
            'batch_count': _count(db, Batch, Batch.period == period_obj.period),
        }
    )
    return info


def list_periods(db):
    periods = db.scalars(select(SettlementPeriod).order_by(SettlementPeriod.period.desc())).all()
    return [serialize_period(db, p) for p in periods]


def get_period(db, period):
    obj = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period))
    if not obj:
        raise HTTPException(404, '账期不存在')
    return serialize_period(db, obj)


def create_period(db, period):
    validate_period_name(period)
    insert_period_if_missing(db, period)
    obj = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period).with_for_update())
    if obj.status == 'closed':
        raise HTTPException(409, '账期已关闭，不能重复开启；迟到账单请走入库补算')
    db.commit()
    return serialize_period(db, obj)


def close_period(db, period):
    existing = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period))
    if not existing:
        raise HTTPException(404, '账期不存在')
    obj = lock_period(db, period)
    if obj.status != 'closed':
        obj.status = 'closed'
        obj.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.flush()
    enqueue = refresh_stability(db, obj)
    db.commit()
    from .ingestion import enqueue_backfills

    enqueue_backfills(enqueue)
    return serialize_period(db, obj)


def get_period_obj(db, period):
    if not period:
        return None
    return db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == period))


def settle_batch(db, batch, evidence, period_obj):
    """按证据状态与水位稳定性收敛批次状态。"""
    if batch is None:
        return
    if evidence.state == 'failed':
        batch.status = 'failed'
        batch.status_reason = (evidence.result or {}).get('error') or '执行失败'
        return
    if evidence.state == 'stale':
        batch.status = 'backfilling'
        batch.status_reason = evidence.reason or '证据因迟到账单失效，等待补算'
        return
    result = evidence.result or {}
    if not result.get('equivalent'):
        batch.status = 'different'
        batch.status_reason = None if watermark_info(db, period_obj)['stable'] else watermark_info(db, period_obj)['reason']
        return
    info = watermark_info(db, period_obj)
    if info['stable'] and evidence.watermark_seq == period_obj.stable_seq:
        batch.status = 'passed'
        batch.status_reason = None
    else:
        batch.status = 'waiting_watermark'
        if evidence.watermark_seq < period_obj.ingest_seq:
            batch.status_reason = f'证据采用水位 {evidence.watermark_seq}，最新入库水位 {period_obj.ingest_seq}，待补算'
        else:
            batch.status_reason = info['reason']


def refresh_stability(db, period_obj):
    """收敛证据水位、创建缺失补算、推进 stable_seq、重算批次状态。

    返回需要在提交后投递的补算批次 id 列表。调用方负责 commit。
    """
    from .ingestion import ensure_backfill

    if period_obj is None:
        return []
    period = period_obj.period
    created = []
    stale = db.scalars(
        select(Evidence)
        .where(
            Evidence.period == period,
            Evidence.state.in_(('valid', 'stale')),
            Evidence.watermark_seq < period_obj.ingest_seq,
        )
        .order_by(Evidence.id)
    ).all()
    for evidence in stale:
        affected = sorted(set(evidence.affected_customers or []))
        backfill = ensure_backfill(
            db,
            period_obj,
            evidence,
            target_seq=period_obj.ingest_seq,
            customers=affected,
            reason=f'证据采用水位 {evidence.watermark_seq}，落后于最新入库水位 {period_obj.ingest_seq}',
        )
        if backfill is not None and backfill.id not in created:
            created.append(backfill.id)
        if backfill is not None and backfill.status != 'done':
            # 证据落后于最新水位：在补算完成前标记失效，避免把旧结果当成最新结果。
            if evidence.state != 'stale':
                evidence.state = 'stale'
                evidence.version = (evidence.version or 1) + 1
            evidence.invalidated_seq = period_obj.ingest_seq
            evidence.reason = f'证据采用水位 {evidence.watermark_seq}，最新入库水位 {period_obj.ingest_seq}，等待补算'

    active = _count(db, Backfill, Backfill.period == period, Backfill.status.in_(BACKFILL_ACTIVE_STATUSES))
    failed = _count(db, Backfill, Backfill.period == period, Backfill.status == 'failed')
    incomplete = _count(
        db,
        Evidence,
        Evidence.period == period,
        Evidence.state != 'failed',
        Evidence.watermark_seq < period_obj.ingest_seq,
    )
    if period_obj.status == 'closed' and active == 0 and failed == 0 and incomplete == 0:
        period_obj.stable_seq = period_obj.ingest_seq

    evidence_rows = db.scalars(select(Evidence).where(Evidence.period == period)).all()
    for evidence in evidence_rows:
        settle_batch(db, db.get(Batch, evidence.batch_id), evidence, period_obj)
    db.flush()
    return created
