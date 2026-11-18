from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError

from ..config.database import get_db
from ..schemas.period import IngestionInput, PeriodInput
from ..services.auth import current_user, writer
from ..services.ingestion import get_backfill, ingest_bills, list_backfills, retry_backfill
from ..services.watermark import close_period, create_period, get_period, list_periods

router = APIRouter()


@router.get('/periods')
def periods(db=Depends(get_db), user=Depends(current_user)):
    return list_periods(db)


@router.get('/periods/{period}')
def period_detail(period: str, db=Depends(get_db), user=Depends(current_user)):
    return get_period(db, period)


@router.post('/periods', status_code=201)
def period_create(data: PeriodInput, db=Depends(get_db), user=Depends(writer)):
    try:
        return create_period(db, data.period)
    except IntegrityError:
        db.rollback()
        from fastapi import HTTPException

        raise HTTPException(409, '账期已存在')


@router.post('/periods/{period}/close')
def period_close(period: str, db=Depends(get_db), user=Depends(writer)):
    return close_period(db, period)


@router.post('/ingestions', status_code=202)
def ingest(data: IngestionInput, db=Depends(get_db), user=Depends(writer)):
    return ingest_bills(db, data.period, data.bills)


@router.get('/backfills')
def backfills(period: str | None = None, db=Depends(get_db), user=Depends(current_user)):
    return list_backfills(db, period)


@router.get('/backfills/{ident}')
def backfill_detail(ident: int, db=Depends(get_db), user=Depends(current_user)):
    return get_backfill(db, ident)


@router.post('/backfills/{ident}/retry', status_code=202)
def backfill_retry(ident: int, db=Depends(get_db), user=Depends(writer)):
    return retry_backfill(db, ident)
