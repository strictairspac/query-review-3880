"""Schema 初始化与轻量迁移（schema v2）。

Base.metadata.create_all 只建缺失表，不会给已有表补列；这里集中做幂等 ALTER，
让旧库升级后仍能支持账期水位、补算与证据版本。
"""
from sqlalchemy import text

from .database import Base, engine

BASELINE_PERIOD = '2026-10'


def migrate(conn):
    conn.execute(
        text(
            'CREATE TABLE IF NOT EXISTS invoices ('
            ' invoice_id INTEGER PRIMARY KEY, customer TEXT NOT NULL, route TEXT NOT NULL,'
            ' amount NUMERIC(12,2) NOT NULL, temperature NUMERIC(4,1) NOT NULL, status TEXT NOT NULL)'
        )
    )
    conn.execute(text('ALTER TABLE invoices ADD COLUMN IF NOT EXISTS settlement_period TEXT'))
    conn.execute(text('ALTER TABLE invoices ADD COLUMN IF NOT EXISTS ingest_seq INTEGER'))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_invoices_period ON invoices (settlement_period)'))

    conn.execute(text("ALTER TABLE batches ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT ''"))
    conn.execute(text('ALTER TABLE batches ADD COLUMN IF NOT EXISTS status_reason TEXT'))
    conn.execute(text('ALTER TABLE batches ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1'))
    conn.execute(text('ALTER TABLE batches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()'))

    conn.execute(text("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT ''"))
    conn.execute(text('ALTER TABLE evidence ADD COLUMN IF NOT EXISTS watermark_seq INTEGER NOT NULL DEFAULT 0'))
    conn.execute(text('ALTER TABLE evidence ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1'))
    conn.execute(text("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'valid'"))
    conn.execute(text("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS affected_customers JSON DEFAULT '[]'"))
    conn.execute(text('ALTER TABLE evidence ADD COLUMN IF NOT EXISTS invalidated_seq INTEGER'))
    conn.execute(text('ALTER TABLE evidence ADD COLUMN IF NOT EXISTS reason TEXT'))

    # 升级前没有账期归属的批次/证据归入演示基线账期，避免恢复循环反复空转。
    conn.execute(text("UPDATE batches SET period = :period WHERE period = ''"), {'period': BASELINE_PERIOD})
    conn.execute(text("UPDATE evidence SET period = :period WHERE period = ''"), {'period': BASELINE_PERIOD})

    conn.execute(
        text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='query_reader') "
            'THEN CREATE ROLE query_reader NOLOGIN; END IF; END $$'
        )
    )
    conn.execute(text('GRANT SELECT ON invoices TO query_reader'))


def ensure_schema():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        migrate(conn)
