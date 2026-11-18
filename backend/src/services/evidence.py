"""证据读取与展示。

- valid：完整结果可查；
- stale：受影响客户的旧行作废并隐藏，只有未受影响客户继续可查，页面标注“补算”和原因；
- failed：无有效结果。
证据 JSON 始终还原该结果实际采用的账期入库水位。
"""
import json

from fastapi import HTTPException
from sqlalchemy import select

from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.period import SettlementPeriod
from .watermark import watermark_info


def _split_rows(side, affected, watermark_state):
    """按受影响客户拆分可见行与待补算行。

    输出没有 customer 列时无法判断哪些行受影响，保守地把整份结果标为待补算，
    避免把可能失效的汇总值当成可查结果。
    """
    if side is None:
        return None
    columns = side.get('columns') or []
    try:
        index = columns.index('customer')
    except ValueError:
        view = dict(side)
        view['rows'] = []
        view['row_count'] = 0
        view['pending_rows'] = list(side.get('rows') or [])
        view['pending_row_count'] = len(view['pending_rows'])
        view['partial'] = watermark_state == 'stale'
        view['pending_reason'] = '输出不含 customer 列，无法只隐藏受影响客户，整份结果待补算'
        return view
    affected = set(affected or [])
    visible, pending = [], []
    for row in side.get('rows') or []:
        if len(row) > index and row[index] in affected:
            pending.append(row)
        else:
            visible.append(row)
    view = dict(side)
    view['rows'] = visible
    view['row_count'] = len(visible)
    view['pending_rows'] = pending
    view['pending_row_count'] = len(pending)
    view['partial'] = watermark_state == 'stale'
    return view


def present(db, evidence):
    batch = db.get(Batch, evidence.batch_id)
    period_obj = db.scalar(select(SettlementPeriod).where(SettlementPeriod.period == evidence.period))
    info = watermark_info(db, period_obj)
    result = evidence.result or {}
    cloned = json.loads(json.dumps(result))
    stale = evidence.state == 'stale'
    affected = evidence.affected_customers or []
    payload = {
        'id': evidence.id,
        'batch_id': evidence.batch_id,
        'period': evidence.period,
        'state': evidence.state,
        'watermark_seq': evidence.watermark_seq,
        'affected_customers': affected,
        'reason': evidence.reason,
        'version': evidence.version,
        'watermark': info,
        'batch_status': batch.status if batch else None,
        'batch_status_reason': batch.status_reason if batch else None,
        'result': cloned,
    }
    if not stale:
        return payload
    # 失效证据：未受影响客户仍可查，受影响客户显示待补算原因。
    cloned['original'] = _split_rows(cloned.get('original'), affected, evidence.state)
    cloned['candidate'] = _split_rows(cloned.get('candidate'), affected, evidence.state)
    cloned['stale'] = True
    cloned['affected_customers'] = affected
    cloned['stale_reason'] = evidence.reason or '迟到账单入库，受影响客户等待补算'
    return payload


def list_evidence(db):
    return [present(db, x) for x in db.scalars(select(Evidence).order_by(Evidence.id.desc()))]


def get_evidence(db, batch_id):
    x = db.scalar(select(Evidence).where(Evidence.batch_id == batch_id))
    if not x:
        raise HTTPException(404, '证据尚未生成，请等待 worker 完成')
    return present(db, x)


def export_payload(item):
    """下载内容：结果、水位与还原说明。"""
    return {
        'batch_id': item['batch_id'],
        'period': item['period'],
        'state': item['state'],
        'watermark': {
            'ingest_seq': item['watermark_seq'],
            'stable_seq': (item['watermark'] or {}).get('stable_seq'),
            'period_status': (item['watermark'] or {}).get('status'),
            'stable': (item['watermark'] or {}).get('stable'),
            'state': (item['watermark'] or {}).get('state'),
            'label': (item['watermark'] or {}).get('label'),
            'reason': (item['watermark'] or {}).get('reason'),
        },
        'restore_note': f"本证据结果对应的账期入库水位为 {item['watermark_seq']}；复跑时用该水位还原数据范围。",
        'affected_customers': item['affected_customers'],
        'reason': item['reason'],
        'result': item['result'],
    }
