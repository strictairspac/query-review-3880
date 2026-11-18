"""Integration tests for settlement-period watermarks, late bills and backfills.

Requires the real PostgreSQL fixture: set TEST_DATABASE_URL and TEST_REDIS_URL,
then run `pytest tests -q` from backend/. The suite creates its own database
schema objects and cleans up its periods/bills/batches after each test.
"""
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault('DATABASE_URL', os.environ.get('TEST_DATABASE_URL', ''))
os.environ.setdefault('REDIS_URL', os.environ.get('TEST_REDIS_URL', 'redis://127.0.0.1:56379/0'))
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACTS_DIR', '/tmp/artifacts')
os.environ.setdefault('SEED_DEMO', 'false')

pytestmark = pytest.mark.integration


@pytest.fixture(scope='session')
def app_client():
    if not os.environ.get('DATABASE_URL'):
        pytest.skip('TEST_DATABASE_URL 未设置，跳过需要真实数据库的集成测试')
    from scripts.seed_demo import seed

    seed()
    _purge_test_data()
    from src.main import app

    with TestClient(app) as client:
        yield client


def _purge_test_data():
    """清掉历史测试遗留的批次/账期/迟到账单，保证每次运行从干净基线开始。"""
    from sqlalchemy import text

    from src.config.database import engine

    with engine.begin() as conn:
        conn.execute(text('DELETE FROM backfill_batches'))
        conn.execute(text('DELETE FROM bill_ingestions'))
        conn.execute(text('DELETE FROM evidence'))
        conn.execute(text('DELETE FROM batches'))
        conn.execute(text("DELETE FROM settlement_periods WHERE period <> '2026-10'"))
        conn.execute(text("DELETE FROM invoices WHERE settlement_period <> '2026-10'"))


@pytest.fixture(scope='session')
def tokens(app_client):
    out = {}
    for role in ('admin', 'operator', 'viewer'):
        response = app_client.post(
            '/auth/login', json={'email': f'{role}@chenggang.test', 'password': 'ColdChain!17'}
        )
        assert response.status_code == 200, response.text
        out[role] = {'Authorization': 'Bearer ' + response.json()['token']}
    return out


@pytest.fixture(scope='session')
def invoice_id():
    """跨测试运行也不冲突的账单号，避免重复跑套件时撞上历史数据。"""
    base = uuid.uuid4().int % 1000000 + 1_000_000

    class Ids:
        def __init__(self):
            self.next = base

        def __call__(self):
            self.next += 1
            return self.next

    return Ids()


@pytest.fixture()
def period():
    """每个测试独立账期，避免同一次运行内复用已关闭账期。"""
    counter = _next_period()
    return f'21{counter:02d}-11'


_period_counter = [0]


def _next_period():
    _period_counter[0] += 1
    return _period_counter[0]


@pytest.fixture()
def db():
    from src.config.database import Session

    with Session() as session:
        yield session


def _new_period(client, headers, name):
    response = client.post('/periods', headers=headers, json={'period': name})
    assert response.status_code in (201, 409), response.text


def _create_batch(client, headers, period, name, candidate=None):
    payload = {
        'name': name,
        'sample_id': 1,
        'period': period,
        'candidate_sql': candidate
        or 'SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 '
        'GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer',
    }
    response = client.post('/batches', headers=headers, json=payload)
    assert response.status_code == 202, response.text
    return response.json()['id']


def _run_review(batch_id):
    from src.worker import run_one

    run_one(batch_id)


def _run_backfill(backfill_id):
    from src.services.backfill import run_backfill

    return run_backfill(backfill_id)


def _batch(client, headers, batch_id):
    return next(x for x in client.get('/batches', headers=headers).json() if x['id'] == batch_id)


def _evidence(client, headers, batch_id):
    return client.get(f'/evidence/{batch_id}', headers=headers).json()


def _ingest(client, headers, period, bills):
    response = client.post('/ingestions', headers=headers, json={'period': period, 'bills': bills})
    return response


def _bill(ref, invoice_id, customer, amount=100, temperature=5):
    return {
        'external_ref': ref,
        'invoice_id': invoice_id,
        'customer': customer,
        'route': '东线',
        'amount': amount,
        'temperature': temperature,
        'status': 'settled',
    }


@pytest.fixture()
def closed_period_with_passed_evidence(app_client, tokens, period, invoice_id):
    """一个有基线账单的关闭账期 + 已通过证据，供迟到账单场景复用。"""
    headers = tokens['operator']
    _new_period(app_client, headers, period)
    baseline = _ingest(
        app_client,
        headers,
        period,
        [
            _bill('BASE-CG', invoice_id(), '澄港海产', 1000),
            _bill('BASE-NG', invoice_id(), '南港鲜食', 1200),
            _bill('BASE-BW', invoice_id(), '北湾冷运', 1400),
        ],
    )
    assert baseline.status_code == 202, baseline.text
    batch_id = _create_batch(app_client, headers, period, f'base-{uuid.uuid4().hex[:8]}')
    _run_review(batch_id)
    assert _batch(app_client, headers, batch_id)['status'] == 'waiting_watermark'
    closed = app_client.post(f'/periods/{period}/close', headers=headers)
    assert closed.status_code == 200, closed.text
    assert _batch(app_client, headers, batch_id)['status'] == 'passed'
    return {'period': period, 'batch_id': batch_id, 'headers': headers, 'ingest_seq': 3}


def test_open_period_cannot_publish_pass(app_client, tokens, period):
    headers = tokens['operator']
    _new_period(app_client, headers, period)
    batch_id = _create_batch(app_client, headers, period, f'wait-{uuid.uuid4().hex[:8]}')
    _run_review(batch_id)
    item = _batch(app_client, headers, batch_id)
    assert item['status'] == 'waiting_watermark'
    assert '账期尚未关闭' in item['status_reason']
    evidence = _evidence(app_client, headers, batch_id)
    assert evidence['result']['equivalent'] is True
    assert evidence['watermark_seq'] == 0


def test_late_bill_invalidates_only_affected_customers(
    app_client, closed_period_with_passed_evidence, invoice_id
):
    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    before = _evidence(app_client, headers, batch_id)
    unaffected = sorted({row[0] for row in before['result']['original']['rows']} - {'澄港海产'})
    assert unaffected

    response = _ingest(app_client, headers, period, [_bill('LATE-A', invoice_id(), '澄港海产', 999)])
    assert response.status_code == 202, response.text
    body = response.json()
    assert body['late'] is True
    assert body['ingest_seq'] == ctx['ingest_seq'] + 1
    assert body['affected_customers'] == ['澄港海产']
    assert body['watermark']['stable'] is False
    assert body['watermark']['state'] == 'backfilling'

    stale = _evidence(app_client, headers, batch_id)
    assert stale['state'] == 'stale'
    assert '澄港海产' in stale['affected_customers']
    visible = {row[0] for row in stale['result']['original']['rows']}
    assert visible == set(unaffected), '未受影响客户必须继续可查'
    pending = {row[0] for row in stale['result']['original']['pending_rows']}
    assert pending == {'澄港海产'}
    assert _batch(app_client, headers, batch_id)['status'] == 'backfilling'

    # 补算只重算受影响客户，完成后水位稳定、摘要恢复可查。
    backfill_id = body['backfills'][0]['id']
    _run_backfill(backfill_id)
    evidence = _evidence(app_client, headers, batch_id)
    assert evidence['state'] == 'valid'
    assert evidence['watermark_seq'] == ctx['ingest_seq'] + 1
    assert {row[0] for row in evidence['result']['original']['rows']} == set(unaffected) | {'澄港海产'}
    assert evidence['result']['backfill']['mode'] == 'customers'
    assert _batch(app_client, headers, batch_id)['status'] == 'passed'
    watermark = app_client.get(f'/periods/{period}', headers=headers).json()
    assert watermark['stable'] is True and watermark['stable_seq'] == ctx['ingest_seq'] + 1


def test_duplicate_ingestion_is_idempotent(app_client, closed_period_with_passed_evidence, invoice_id):
    ctx = closed_period_with_passed_evidence
    headers, period = ctx['headers'], ctx['period']
    first = _ingest(app_client, headers, period, [_bill('LATE-DUP', invoice_id(), '南港鲜食')]).json()
    again = _ingest(app_client, headers, period, [_bill('LATE-DUP', invoice_id(), '南港鲜食')])
    assert again.status_code == 202
    body = again.json()
    assert body['created'] == 0
    assert body['ingest_seq'] == first['ingest_seq']
    assert body['skipped'][0]['reason'].startswith('已入库')
    assert body['backfills'] == []


def test_restore_replays_evidence_watermark(app_client, closed_period_with_passed_evidence, invoice_id):
    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    _ingest(app_client, headers, period, [_bill('LATE-R1', invoice_id(), '北湾冷运', 500)])
    backfill = app_client.get(f'/backfills?period={period}', headers=headers).json()[0]
    _run_backfill(backfill['id'])
    evidence = _evidence(app_client, headers, batch_id)
    restored = app_client.get(f'/evidence/{batch_id}/restore', headers=headers)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body['watermark_seq'] == evidence['watermark_seq']
    assert body['matches_evidence'] is True
    export = app_client.get(f'/evidence/{batch_id}/export', headers=headers)
    payload = json.loads(export.text)
    assert payload['watermark']['ingest_seq'] == evidence['watermark_seq']
    assert '水位' in payload['restore_note']


def test_viewer_cannot_write_but_can_read(app_client, tokens, closed_period_with_passed_evidence, invoice_id):
    viewer = tokens['viewer']
    ctx = closed_period_with_passed_evidence
    response = _ingest(app_client, viewer, ctx['period'], [_bill('LATE-V', invoice_id(), '澄港海产')])
    assert response.status_code == 403
    assert app_client.post(f'/periods/{ctx["period"]}/close', headers=viewer).status_code == 403
    assert app_client.get(f'/periods/{ctx["period"]}', headers=viewer).status_code == 200
    assert app_client.get(f'/evidence/{ctx["batch_id"]}', headers=viewer).status_code == 200
    assert app_client.get(f'/evidence/{ctx["batch_id"]}/export', headers=viewer).status_code == 200


def test_cross_period_isolation(app_client, tokens, invoice_id):
    headers = tokens['operator']
    target = f'21{_next_period():02d}-11'
    other = f'21{_next_period():02d}-12'
    _new_period(app_client, headers, target)
    _new_period(app_client, headers, other)
    target_batch = _create_batch(app_client, headers, target, f'iso-a-{uuid.uuid4().hex[:8]}')
    _run_review(target_batch)
    app_client.post(f'/periods/{target}/close', headers=headers)
    # 另一个账期随后入账 99999：不得混入 target 的结果。
    assert _ingest(app_client, headers, other, [_bill('OTHER-1', invoice_id(), '澄港海产', 99999)]).status_code == 202
    rows = _evidence(app_client, headers, target_batch)['result']['original']['rows']
    assert rows == [], '账期隔离后，其他账期的账单不得进入本账期结果'
    evidence = _evidence(app_client, headers, target_batch)
    assert evidence['watermark_seq'] == 0
    assert app_client.get(f'/periods/{target}', headers=headers).json()['stable'] is True


def test_backfill_version_conflict_does_not_overwrite(db, app_client, tokens, period, invoice_id):
    headers = tokens['operator']
    _new_period(app_client, headers, period)
    bill_batch = _create_batch(app_client, headers, period, f'cc-{uuid.uuid4().hex[:8]}')
    _run_review(bill_batch)
    app_client.post(f'/periods/{period}/close', headers=headers)
    _ingest(app_client, headers, period, [_bill('LATE-V', invoice_id(), '澄港海产')])
    backfill = app_client.get(f'/backfills?period={period}', headers=headers).json()[0]
    # 模拟并发的更新复核先提交：水位与版本都前进。
    db.execute(
        text(
            'UPDATE evidence SET version = version + 5, watermark_seq = watermark_seq + 5 '
            'WHERE batch_id = :batch_id'
        ),
        {'batch_id': bill_batch},
    )
    db.commit()
    _run_backfill(backfill['id'])
    row = db.execute(
        text('SELECT version, watermark_seq FROM evidence WHERE batch_id = :batch_id'), {'batch_id': bill_batch}
    ).one()
    # 补算发现证据水位已领先，不应覆盖并发写入的结果。
    assert row.watermark_seq >= 5
    record = db.execute(text('SELECT status FROM backfill_batches WHERE id = :id'), {'id': backfill['id']}).scalar()
    assert record in ('done', 'interrupted', 'queued')


def test_backfill_retry_not_double_counted(app_client, closed_period_with_passed_evidence, invoice_id):
    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    first = _ingest(app_client, headers, period, [_bill('RETRY-1', invoice_id(), '澄港海产', 700)]).json()
    backfill_id = first['backfills'][0]['id']
    _run_backfill(backfill_id)
    once = _evidence(app_client, headers, batch_id)['result']['original_checksum']
    # 重复投递同一个补算：水位已达标，直接 done，结果不变。
    _run_backfill(backfill_id)
    twice = _evidence(app_client, headers, batch_id)['result']['original_checksum']
    assert once == twice
    assert _evidence(app_client, headers, batch_id)['watermark_seq'] == ctx['ingest_seq'] + 1


def test_worker_recovers_interrupted_backfill(db, app_client, closed_period_with_passed_evidence, invoice_id):
    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    body = _ingest(app_client, headers, period, [_bill('INT-1', invoice_id(), '北湾冷运', 300)]).json()
    backfill_id = body['backfills'][0]['id']
    from src.models.backfill import Backfill

    obj = db.get(Backfill, backfill_id)
    obj.status = 'running'
    obj.attempts = 1
    db.commit()
    from src.worker import _requeue_interrupted, run_backfill_one

    _requeue_interrupted()
    run_backfill_one(backfill_id)
    evidence = _evidence(app_client, headers, batch_id)
    assert evidence['state'] == 'valid'
    assert evidence['watermark_seq'] == ctx['ingest_seq'] + 1
    db.expire_all()
    assert db.get(Backfill, backfill_id).status == 'done'


def test_concurrent_new_review_and_backfill_do_not_overwrite(
    db, app_client, closed_period_with_passed_evidence, invoice_id
):
    """补算与新复核并发：两边各自提交，不得互相覆盖对方的证据。"""
    import threading

    from src.worker import run_one

    ctx = closed_period_with_passed_evidence
    headers, period, old_batch = ctx['headers'], ctx['period'], ctx['batch_id']
    body = _ingest(app_client, headers, period, [_bill('CONC-1', invoice_id(), '澄港海产', 250)]).json()
    backfill_id = body['backfills'][0]['id']
    new_batch = _create_batch(app_client, headers, period, f'conc-{uuid.uuid4().hex[:8]}')

    errors = []

    def work(fn, arg):
        try:
            fn(arg)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(_run_backfill, backfill_id))]
    threads.append(threading.Thread(target=work, args=(run_one, new_batch)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors

    old_evidence = _evidence(app_client, headers, old_batch)
    new_evidence = _evidence(app_client, headers, new_batch)
    # 旧证据由补算更新到最新水位，新证据采用入队时水位；两者都完整可查。
    assert old_evidence['state'] == 'valid'
    assert old_evidence['watermark_seq'] == ctx['ingest_seq'] + 1
    assert new_evidence['state'] == 'valid'
    assert new_evidence['result']['equivalent'] is True
    assert old_evidence['result']['original_checksum']
    assert new_evidence['result']['original_checksum']
    # 每个批次只有一条证据，没有互相覆盖导致的重复或缺失。
    rows = db.execute(
        text('SELECT batch_id, COUNT(*) FROM evidence WHERE batch_id IN (:a, :b) GROUP BY batch_id'),
        {'a': old_batch, 'b': new_batch},
    ).all()
    assert {row[0] for row in rows} == {old_batch, new_batch}
    assert all(row[1] == 1 for row in rows)


def test_backfill_interruption_then_recovery_is_not_double_counted(
    db, app_client, closed_period_with_passed_evidence, invoice_id
):
    """补算进程中断后恢复：重试同一批次，结果不重复计入。"""
    from src.models.backfill import Backfill
    from src.worker import _requeue_interrupted, run_backfill_one

    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    body = _ingest(app_client, headers, period, [_bill('ABORT-1', invoice_id(), '南港鲜食', 400)]).json()
    backfill_id = body['backfills'][0]['id']
    # 模拟执行到一半进程被杀：状态停在 running，证据仍是旧水位。
    obj = db.get(Backfill, backfill_id)
    obj.status = 'running'
    obj.attempts = 1
    db.commit()
    stale = _evidence(app_client, headers, batch_id)
    assert stale['state'] == 'stale'

    _requeue_interrupted()
    run_backfill_one(backfill_id)
    first = _evidence(app_client, headers, batch_id)
    assert first['state'] == 'valid'
    checksum = first['result']['original_checksum']

    # 再次恢复并重跑：不能把同一笔迟到账单再加一次。
    _requeue_interrupted()
    run_backfill_one(backfill_id)
    second = _evidence(app_client, headers, batch_id)
    assert second['result']['original_checksum'] == checksum
    assert second['watermark_seq'] == ctx['ingest_seq'] + 1
    db.expire_all()
    assert db.get(Backfill, backfill_id).status == 'done'


def test_cross_customer_backfills_concurrent_keep_both_results(
    db, app_client, closed_period_with_passed_evidence, invoice_id
):
    """两个客户先后迟到、补算并发执行：结果不能互相覆盖，两位客户都要计入。"""
    import threading

    from src.services.backfill import run_backfill

    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    first = _ingest(app_client, headers, period, [_bill('X-CG', invoice_id(), '澄港海产', 300)]).json()
    second = _ingest(app_client, headers, period, [_bill('X-BW', invoice_id(), '北湾冷运', 500)]).json()
    backfill_a = first['backfills'][0]['id']
    backfill_b = second['backfills'][0]['id']
    assert backfill_a != backfill_b
    assert second['ingest_seq'] == ctx['ingest_seq'] + 2

    errors = []

    def work(ident):
        try:
            run_backfill(ident)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(backfill_a,)), threading.Thread(target=work, args=(backfill_b,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors

    evidence = _evidence(app_client, headers, batch_id)
    assert evidence['watermark_seq'] == ctx['ingest_seq'] + 2
    assert evidence['state'] == 'valid'
    totals = {row[0]: float(row[1]) for row in evidence['result']['original']['rows']}
    assert totals['澄港海产'] == pytest.approx(1300.0)
    assert totals['北湾冷运'] == pytest.approx(1900.0)
    assert totals['南港鲜食'] == pytest.approx(1200.0)
    watermark = app_client.get(f'/periods/{period}', headers=headers).json()
    assert watermark['stable'] is True and watermark['stable_seq'] == ctx['ingest_seq'] + 2


def test_failed_backfill_can_be_retried_manually(db, app_client, closed_period_with_passed_evidence, invoice_id):
    """补算失败后水位保持未稳定；人工重试后完成，且不重复计入。"""
    from src.models.backfill import Backfill

    ctx = closed_period_with_passed_evidence
    headers, period, batch_id = ctx['headers'], ctx['period'], ctx['batch_id']
    body = _ingest(app_client, headers, period, [_bill('FAIL-1', invoice_id(), '北湾冷运', 350)]).json()
    backfill_id = body['backfills'][0]['id']
    obj = db.get(Backfill, backfill_id)
    obj.status = 'failed'
    obj.error = '模拟补算失败'
    db.commit()
    watermark = app_client.get(f'/periods/{period}', headers=headers).json()
    assert watermark['stable'] is False
    assert '失败' in watermark['reason']
    assert app_client.post(f'/backfills/{backfill_id}/retry', headers=tokens_of(app_client, 'viewer')).status_code == 403
    retried = app_client.post(f'/backfills/{backfill_id}/retry', headers=headers)
    assert retried.status_code == 202, retried.text
    assert retried.json()['status'] == 'queued'
    # worker 恢复循环会执行该补算。
    from src.worker import run_backfill_one

    run_backfill_one(backfill_id)
    evidence = _evidence(app_client, headers, batch_id)
    assert evidence['state'] == 'valid'
    assert evidence['watermark_seq'] == ctx['ingest_seq'] + 1
    assert app_client.get(f'/periods/{period}', headers=headers).json()['stable'] is True


def tokens_of(client, role):
    response = client.post('/auth/login', json={'email': f'{role}@chenggang.test', 'password': 'ColdChain!17'})
    return {'Authorization': 'Bearer ' + response.json()['token']}
