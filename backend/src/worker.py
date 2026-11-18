"""独立 worker：消费复核队列与补算队列，并恢复中断任务。

幂等与恢复：
- 已有证据的复核直接跳过；
- 补算按 Backfill 状态与证据水位推进，重复投递不会重复计入；
- 进程中断后，queued/running 的补算会被重新排队，由证据水位和 version 条件收敛。
"""
import json
import time
import traceback
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from .config.database import Session
from .models.backfill import Backfill
from .models.batch import Batch
from .models.evidence import Evidence
from .models.period import SettlementPeriod
from .models.sample import Sample
from .services.backfill import safe_run_backfill
from .config.settings import ARTIFACTS_DIR
from .services.batch import BACKFILL_QUEUE_KEY, QUEUE_KEY, queue
from .services.query_engine import compare
from .services.watermark import get_period_obj, refresh_stability, settle_batch, watermark_info

heartbeat = Path(ARTIFACTS_DIR) / 'worker.heartbeat'
RECOVERY_SECONDS = 15


def _period_of(db, batch):
    return get_period_obj(db, batch.period)


def run_backfill_one(backfill_id):
    try:
        safe_run_backfill(backfill_id)
    except Exception:
        traceback.print_exc()
        with Session() as db:
            obj = db.get(Backfill, backfill_id)
            if obj is not None and obj.status not in ('done', 'failed'):
                obj.status = 'queued'
                obj.error = '执行异常，稍后重试'
                db.commit()


def run_one(batch_id):
    with Session() as db:
        batch = db.get(Batch, batch_id)
        if not batch:
            return
        if db.scalar(select(Evidence).where(Evidence.batch_id == batch_id)):
            if batch.status in ('queued', 'running'):
                refresh_stability(db, _period_of(db, batch))
                db.commit()
            return
        # 认领批次：只有从 queued 抢到 running 的 worker 才执行，避免重复执行。
        # 已有 running 说明是中断恢复；恢复循环会先把它改回 queued 再调用这里。
        claimed = db.execute(
            text(
                "UPDATE batches SET status='running', status_reason=NULL "
                "WHERE id=:id AND status='queued'"
            ),
            {'id': batch_id},
        ).rowcount
        db.commit()
        if not claimed:
            db.expire_all()
            batch = db.get(Batch, batch_id)
            if not batch or db.scalar(select(Evidence).where(Evidence.batch_id == batch_id)):
                return
            if batch.status != 'running':
                return
            # running 但没有证据：可能是上一次执行中断，继续执行即可（插入仍受唯一约束保护）。
        db.refresh(batch)
        if db.scalar(select(Evidence).where(Evidence.batch_id == batch_id)):
            return
        period_obj = _period_of(db, batch)
        watermark_seq = period_obj.ingest_seq if period_obj else 0
        sample = db.get(Sample, batch.sample_id)
        try:
            # 只使用批次启动时看到的水位；之后到达的迟到账单由补算处理。
            result = compare(sample.original_sql, batch.candidate_sql, period=batch.period, watermark=watermark_seq)
            state = 'valid'
            error = None
        except Exception as exc:
            result = {'error': str(exc)[:500]}
            state = 'failed'
            error = str(exc)[:500]
        db.rollback()
        batch = db.get(Batch, batch_id)
        if db.scalar(select(Evidence).where(Evidence.batch_id == batch_id)):
            # 另一个 worker 已先写入证据：不重复插入，也不覆盖。
            return
        period_obj = _period_of(db, batch)
        evidence = Evidence(
            batch_id=batch_id,
            period=batch.period,
            result=result,
            watermark_seq=watermark_seq,
            state=state,
            affected_customers=[],
            reason=None if state != 'failed' else error,
        )
        db.add(evidence)
        batch.status = 'failed' if state == 'failed' else 'waiting_watermark'
        batch.status_reason = error
        try:
            db.flush()
            settle_batch(db, batch, evidence, period_obj)
            to_enqueue = refresh_stability(db, period_obj)
            db.commit()
        except IntegrityError:
            db.rollback()
            return
        review_file = Path(ARTIFACTS_DIR) / f'review-{batch_id}.json'
        review_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    from .services.ingestion import enqueue_backfills

    enqueue_backfills(to_enqueue)


def _requeue_interrupted():
    """恢复中断：running 的复核回到 queued，补算重新排队（不重复计入）。"""
    with Session() as db:
        db.execute(text("UPDATE batches SET status='queued', status_reason='worker 中断后恢复' WHERE status='running'"))
        for obj in db.scalars(select(Backfill).where(Backfill.status.in_(('running', 'interrupted')))):
            obj.status = 'queued'
            obj.error = 'worker 中断后恢复'
        db.commit()
        pending_reviews = list(db.scalars(select(Batch.id).where(Batch.status == 'queued')))
        pending_backfills = list(db.scalars(select(Backfill.id).where(Backfill.status == 'queued')))
        return pending_reviews, pending_backfills


def _recover_stale(period_id):
    with Session() as db:
        period_obj = db.get(SettlementPeriod, period_id)
        if period_obj:
            refresh_stability(db, period_obj)
            db.commit()


def run_backfill_one(backfill_id):
    try:
        safe_run_backfill(backfill_id)
    except Exception:
        traceback.print_exc()
        with Session() as db:
            obj = db.get(Backfill, backfill_id)
            if obj is not None and obj.status not in ('done', 'failed'):
                obj.status = 'queued'
                obj.error = '执行异常，稍后重试'
                db.commit()


def main():
    last_recovery = 0.0
    while True:
        try:
            heartbeat.write_text(str(time.time()))
            item = queue.blpop([QUEUE_KEY, BACKFILL_QUEUE_KEY], timeout=2)
            if item:
                key, raw = item
                if key.decode() == QUEUE_KEY:
                    run_one(int(raw))
                else:
                    run_backfill_one(int(raw))
                continue
            now = time.time()
            if now - last_recovery < RECOVERY_SECONDS:
                time.sleep(0.5)
                continue
            last_recovery = now
            reviews, backfills = _requeue_interrupted()
            for ident in reviews:
                run_one(ident)
            for ident in backfills:
                run_backfill_one(ident)
            with Session() as db:
                period_ids = list(db.scalars(select(SettlementPeriod.id)))
            for ident in period_ids:
                _recover_stale(ident)
        except Exception:
            traceback.print_exc()
            time.sleep(2)


if __name__ == '__main__':
    main()
