from fastapi import HTTPException
from redis import Redis
from sqlalchemy import select

from ..config.settings import REDIS_URL
from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.period import SettlementPeriod
from ..models.sample import Sample
from .query_engine import validate_sql
from .watermark import ensure_period, watermark_info

queue = Redis.from_url(REDIS_URL)

QUEUE_KEY = 'query-review:jobs'
BACKFILL_QUEUE_KEY = 'query-review:backfills'


def serialize(db, x):
    period_obj = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == x.period)) if x.period else None
    info = watermark_info(db, period_obj) if period_obj else None
    evidence = db.scalar(select(Evidence).where(Evidence.batch_id == x.id))
    return {
        'id': x.id,
        'name': x.name,
        'sample_id': x.sample_id,
        'period': x.period,
        'candidate_sql': x.candidate_sql,
        'status': x.status,
        'status_label': status_label(x.status),
        'status_reason': x.status_reason,
        'watermark': info,
        'created_at': str(x.created_at),
        'updated_at': str(x.updated_at),
        'evidence_state': evidence.state if evidence else None,
        'evidence_watermark_seq': evidence.watermark_seq if evidence else None,
        'affected_customers': (evidence.affected_customers or []) if evidence else [],
    }


def status_label(status):
    return {
        'queued': '排队中',
        'running': '执行中',
        'waiting_watermark': '待等水位',
        'passed': '结果一致',
        'different': '存在差异',
        'failed': '执行失败',
        'backfilling': '补算中',
    }.get(status, status)


def list_batches(db):
    return [serialize(db, x) for x in db.scalars(select(Batch).order_by(Batch.id.desc()))]


def create_batch(db, data):
    validate_sql(data.candidate_sql)
    if not db.get(Sample, data.sample_id):
        raise HTTPException(404, '样本不存在')
    if db.scalar(select(Batch).where(Batch.name == data.name)):
        raise HTTPException(409, '批次名称已存在')
    pending = db.scalars(select(Batch).where(Batch.status.in_(['queued', 'running']))).all()
    if len(pending) >= 20:
        raise HTTPException(429, '待执行任务达到20个，请稍后重试')
    ensure_period(db, data.period)
    obj = Batch(**data.model_dump())
    db.add(obj)
    db.commit()
    queue.rpush(QUEUE_KEY, obj.id)
    return serialize(db, obj)
