"""补算执行：只重算受影响客户，乐观版本提交，幂等重试。

并发约定：
- 每个证据行带 version，补算读取 version 后只能写入 version+1；
- 若期间有其他写入（例如更新的补算或复核收敛）先提交，本次写入返回 0 行，
  补算标记 interrupted 后重试，绝不覆盖别人的结果；
- 若证据的水位已经达到或超过本次目标，直接视为已被更新的补算覆盖，不重复计入。
"""
import copy
import json
import time

from sqlalchemy import select, text, update

from ..config.database import Session
from ..models.backfill import Backfill
from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.period import SettlementPeriod
from ..models.sample import Sample
from ..models.ingestion import BillIngestion
from .query_engine import execute_query, summarize, validate_sql
from .watermark import get_period_obj, lock_period, refresh_stability

MAX_ATTEMPTS = 5


def _customer_index(columns):
    try:
        return columns.index('customer')
    except ValueError:
        return None


def _partial_scope_safe(sql):
    """输出含 customer 时，只有所有子查询都与客户相关，才能只重算受影响客户。"""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(validate_sql(sql), read='postgres')
    selects = list(tree.find_all(exp.Select))
    for select in selects:
        if select is tree:
            continue
        if not any(col.name == 'customer' for col in select.find_all(exp.Column)):
            return False
    return True


def _merge_side(old_side, new_side, affected):
    columns = new_side['columns']
    index = _customer_index(columns)
    if index is None:
        return copy.deepcopy(new_side), False
    affected = set(affected)
    kept = [row for row in old_side.get('rows', []) if len(row) > index and row[index] not in affected]
    fresh = [row for row in new_side.get('rows', []) if len(row) > index and row[index] in affected]
    fresh.sort(key=lambda row: json.dumps(row, ensure_ascii=False))
    merged = dict(new_side)
    merged['rows'] = kept + fresh
    merged['row_count'] = len(merged['rows'])
    merged['partial'] = True
    return merged, True


def recompute(original_sql, candidate_sql, period, affected, old_result, watermark):
    """按受影响客户合并结果；查询形态不允许时退回全量重算。

    watermark 为本次补算的目标入库水位：只纳入该水位及之前入库的账单，
    保证结果与证据记录的 watermark_seq 一致，重试不会把后来的账单重复计入。
    """
    old_original = old_result.get('original') or {}
    old_candidate = old_result.get('candidate') or {}
    can_partial = (
        affected
        and old_original.get('columns')
        and old_candidate.get('columns')
        and _customer_index(old_original['columns']) is not None
        and _customer_index(old_candidate['columns']) is not None
        and _partial_scope_safe(original_sql)
        and _partial_scope_safe(candidate_sql)
    )
    if can_partial:
        fresh_original = execute_query(original_sql, period=period, watermark=watermark, customers=affected)
        fresh_candidate = execute_query(candidate_sql, period=period, watermark=watermark, customers=affected)
        merged_original, _ = _merge_side(old_original, fresh_original, affected)
        merged_candidate, _ = _merge_side(old_candidate, fresh_candidate, affected)
        mode = 'customers'
    else:
        merged_original = execute_query(original_sql, period=period, watermark=watermark)
        merged_candidate = execute_query(candidate_sql, period=period, watermark=watermark)
        mode = 'full'
    summary = summarize(merged_original, merged_candidate)
    summary['scope'] = old_result.get('scope') or {'period': period}
    summary['backfill'] = {
        'mode': mode,
        'affected_customers': list(affected),
        'completed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    return summary, mode


def customers_since(db, period, after_seq, upto_seq):
    rows = db.scalars(
        select(BillIngestion.customer).where(
            BillIngestion.period == period,
            BillIngestion.seq > after_seq,
            BillIngestion.seq <= upto_seq,
        )
    ).all()
    return sorted(set(rows))


def _evidence_values(result, watermark_seq, state, affected, reason):
    return {
        'result': result,
        'watermark_seq': watermark_seq,
        'state': state,
        'affected_customers': affected,
        'invalidated_seq': None if state == 'valid' else watermark_seq,
        'reason': reason,
    }


def _retry_later(backfill_id, message):
    """乐观冲突：保留别人的结果，登记重试，不覆盖。"""
    with Session() as db:
        obj = db.get(Backfill, backfill_id)
        if obj is None:
            return
        obj.status = 'interrupted'
        obj.error = message[:500]
        db.commit()


def retry_after_deadlock(backfill_id):
    """死锁/序列化冲突时回滚并重新排队；结果由水位与版本条件保证不重复。"""
    with Session() as db:
        obj = db.get(Backfill, backfill_id)
        if obj is None or obj.status in ('done', 'failed'):
            return
        obj.status = 'queued'
        obj.error = '并发冲突，已重新排队'
        db.commit()
    from .ingestion import enqueue_backfills

    enqueue_backfills([backfill_id])


def safe_run_backfill(backfill_id):
    """worker 入口：吞掉死锁等瞬时错误，交给恢复循环重试。"""
    from sqlalchemy.exc import DBAPIError

    try:
        return run_backfill(backfill_id)
    except DBAPIError as exc:
        if getattr(exc.orig, 'pgcode', None) in ('40P01', '40001'):
            retry_after_deadlock(backfill_id)
            return None
        raise


def run_backfill(backfill_id):
    """执行一条补算。可被重复调用，结果由水位与版本条件保证幂等。

    加锁顺序固定为：账期行 → 证据行 → 补算行，避免并发补算互相死锁。
    """
    with Session() as db:
        obj = db.get(Backfill, backfill_id)
        if obj is None or obj.status in ('done', 'failed'):
            return None
        # 先锁账期，串行化同一账期内的补算提交。
        period_obj = lock_period(db, obj.period) if obj.period else None
        db.refresh(obj)
        if obj.status in ('done', 'failed'):
            db.commit()
            return None
        if obj.attempts >= MAX_ATTEMPTS:
            obj.status = 'failed'
            obj.error = obj.error or f'补算尝试超过 {MAX_ATTEMPTS} 次'
            period_obj = get_period_obj(db, obj.period)
            refresh_stability(db, period_obj)
            db.commit()
            return None
        evidence = db.get(Evidence, obj.evidence_id)
        if evidence is None:
            obj.status = 'failed'
            obj.error = '证据不存在'
            period_obj = get_period_obj(db, obj.period)
            refresh_stability(db, period_obj)
            db.commit()
            return None
        period_obj = get_period_obj(db, obj.period)
        current_ingest = period_obj.ingest_seq if period_obj else 0
        # 更新的补算已经完成到本次目标之后：本次已无工作可做，避免重复计入。
        if evidence.state != 'failed' and evidence.watermark_seq >= obj.target_seq:
            obj.status = 'done'
            obj.error = None
            refresh_stability(db, period_obj)
            db.commit()
            return None
        obj.status = 'running'
        obj.attempts += 1
        obj.error = None
        db.commit()
        batch = db.get(Batch, obj.batch_id)
        sample = db.get(Sample, batch.sample_id) if batch else None
        if sample is None or not evidence.result or 'original' not in (evidence.result or {}):
            obj = db.get(Backfill, backfill_id)
            obj.status = 'failed'
            obj.error = '样本或旧证据缺失，无法补算'
            period_obj = get_period_obj(db, obj.period)
            refresh_stability(db, period_obj)
            db.commit()
            return None
        period = obj.period
        target = obj.target_seq
        old_seq = evidence.watermark_seq
        # 受影响客户 = 本补算登记的客户 ∪ (旧水位, 目标水位] 内入库账单的客户。
        # 后者覆盖其他补算被本次超越时的客户，保证并发/乱序都不漏算。
        affected = sorted(set(obj.affected_customers or []) | set(customers_since(db, period, old_seq, target)))
        version = evidence.version
        old_result = copy.deepcopy(evidence.result)
        original_sql = sample.original_sql
        candidate_sql = batch.candidate_sql
        current_ingest = period_obj.ingest_seq if period_obj else current_ingest

    try:
        merged, mode = recompute(original_sql, candidate_sql, period, affected, old_result, target)
        merged['watermark_seq'] = target
        merged['period'] = period
    except Exception as exc:
        with Session() as db:
            obj = db.get(Backfill, backfill_id)
            if obj and obj.status == 'running':
                obj.status = 'failed'
                obj.error = str(exc)[:500]
            period_obj = get_period_obj(db, period)
            refresh_stability(db, period_obj)
            db.commit()
        return None

    with Session() as db:
        # 再次进入执行阶段时也先锁账期，保持与执行阶段一致的加锁顺序。
        period_obj = lock_period(db, period) if period else None
        obj = db.get(Backfill, backfill_id)
        db.refresh(obj)
        if obj.status == 'done':
            db.commit()
            return None
        current_ingest = period_obj.ingest_seq if period_obj else 0
        evidence = db.get(Evidence, obj.evidence_id)
        if evidence is None:
            obj.status = 'failed'
            obj.error = '证据不存在'
            refresh_stability(db, period_obj)
            db.commit()
            return None
        if evidence.watermark_seq >= target and evidence.state != 'failed':
            obj.status = 'done'
            obj.error = None
            refresh_stability(db, period_obj)
            db.commit()
            return None

        lagged = target < current_ingest
        pending = sorted(set(affected) | set(customers_since(db, period, target, current_ingest))) if lagged else []
        state = 'stale' if lagged else 'valid'
        reason = (
            f'补算完成水位 {target}，但账期最新入库水位已到 {current_ingest}，继续等待下一次补算'
            if lagged
            else None
        )
        values = _evidence_values(merged, target, state, pending if lagged else [], reason)
        values['version'] = evidence.version + 1
        written = db.execute(
            update(Evidence)
            .where(Evidence.id == evidence.id, Evidence.version == evidence.version)
            .values(**values)
        ).rowcount
        obj = db.get(Backfill, backfill_id)
        if not written:
            # 别人先提交了更新：不覆盖，保留其结果并登记重试。
            obj.status = 'interrupted'
            obj.error = '并发更新冲突，已保留其他写入结果，稍后重试'
            db.commit()
            return None
        obj.status = 'done'
        obj.error = None
        obj.affected_customers = pending if lagged else []
        to_enqueue = refresh_stability(db, period_obj)
        for nested in db.scalars(
            select(Backfill.id).where(Backfill.period == period, Backfill.status == 'queued')
        ):
            if nested not in to_enqueue:
                to_enqueue.append(nested)
        db.commit()
    from .ingestion import enqueue_backfills

    enqueue_backfills(to_enqueue)
    return mode
