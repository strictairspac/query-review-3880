import json

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, text

from ..config.database import get_db
from ..models.batch import Batch
from ..models.evidence import Evidence
from ..models.sample import Sample
from ..schemas.evidence import EvidenceOutput
from ..services.auth import current_user
from ..services.evidence import export_payload, get_evidence, list_evidence
from ..services.query_engine import execute_query, summarize

router = APIRouter(prefix='/evidence')


@router.get('', response_model=list[EvidenceOutput])
def listing(db=Depends(get_db), user=Depends(current_user)):
    return list_evidence(db)


@router.get('/{batch_id}', response_model=EvidenceOutput)
def detail(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    return get_evidence(db, batch_id)


@router.get('/{batch_id}/export')
def export(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    item = get_evidence(db, batch_id)
    body = export_payload(item)
    return Response(
        json.dumps(body, ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="review-{batch_id}.json"'},
    )


@router.get('/{batch_id}/restore')
def restore(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    """按证据记录的入库水位还原数据范围并重新执行两侧 SQL。

    只纳入 watermark_seq 及之前入库的账单；证据之后到达的迟到账单不参与，
    因此复跑摘要应与证据一致（除非证据本身已因更新的水位被标记 stale）。
    """
    item = get_evidence(db, batch_id)
    batch = db.get(Batch, batch_id)
    sample = db.get(Sample, batch.sample_id) if batch else None
    if not batch or not sample:
        raise HTTPException(404, '批次或样本不存在')
    watermark = item['watermark_seq']
    left = execute_query(sample.original_sql, period=item['period'], watermark=watermark)
    right = execute_query(batch.candidate_sql, period=item['period'], watermark=watermark)
    summary = summarize(left, right)
    evidence_result = item['result'] or {}
    return {
        'batch_id': batch_id,
        'period': item['period'],
        'watermark_seq': watermark,
        'restore_note': f"按账期 {item['period']} 的入库水位 {watermark} 还原数据范围；水位之后的迟到账单不参与。",
        'equivalent': summary['equivalent'],
        'original_checksum': summary['original_checksum'],
        'candidate_checksum': summary['candidate_checksum'],
        'original_row_count': left['row_count'],
        'candidate_row_count': right['row_count'],
        'evidence_original_checksum': evidence_result.get('original_checksum'),
        'evidence_candidate_checksum': evidence_result.get('candidate_checksum'),
        'matches_evidence': summary['original_checksum'] == evidence_result.get('original_checksum')
        and summary['candidate_checksum'] == evidence_result.get('candidate_checksum'),
    }
