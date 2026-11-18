from sqlalchemy import select, text

from src.config.database import Base, Session, engine
from src.config.schema import BASELINE_PERIOD, ensure_schema
from src.models.audit import Audit
from src.models.backfill import Backfill
from src.models.batch import Batch
from src.models.evidence import Evidence
from src.models.ingestion import BillIngestion
from src.models.period import SettlementPeriod
from src.models.sample import Sample
from src.models.user import User
from src.utils.password import hash_password

ORIGINAL = "SELECT DISTINCT i.customer, (SELECT SUM(j.amount) FROM invoices j WHERE j.customer=i.customer AND j.temperature<=8) AS total FROM invoices i WHERE (SELECT SUM(k.amount) FROM invoices k WHERE k.customer=i.customer AND k.temperature<=8)>100 ORDER BY customer"


def seed():
    ensure_schema()
    with engine.begin() as conn:
        # 旧数据没有账期归属时归入固定历史账期，行为与升级前一致。
        conn.execute(
            text('UPDATE invoices SET settlement_period = :period WHERE settlement_period IS NULL'), {'period': BASELINE_PERIOD}
        )
        conn.execute(
            text(
                "INSERT INTO invoices SELECT i, (ARRAY['澄港海产','南港鲜食','北湾冷运'])[1+(i%3)], "
                "(ARRAY['东线','西线'])[1+(i%2)], 35+(i%17)*7, 2+(i%9), "
                "CASE WHEN i%7=0 THEN 'pending' ELSE 'settled' END, :period, NULL "
                'FROM generate_series(1,600) i ON CONFLICT DO NOTHING'
            ),
            {'period': BASELINE_PERIOD},
        )
        conn.execute(
            text(
                'INSERT INTO settlement_periods (period,status,ingest_seq,stable_seq,auto_close) '
                "VALUES (:period,'closed',0,0,false) ON CONFLICT (period) DO NOTHING"
            ),
            {'period': BASELINE_PERIOD},
        )
    with Session() as db:
        for role in ('admin', 'operator', 'viewer'):
            email = role + '@chenggang.test'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, role=role, password_hash=hash_password('ColdChain!17')))
        samples = [
            ('低温结算汇总', '09:00 澄港海产、南港鲜食、北湾冷运的温控合格运费，600 行固定账单。', ORIGINAL),
            (
                '路线平均费用',
                '按路线观察已结算记录的均值；用 ROUND 保持两位小数。',
                "SELECT route, ROUND(AVG(amount),2) AS average FROM invoices WHERE status='settled' GROUP BY route ORDER BY route",
            ),
            (
                '异常温控账单',
                '温度大于8度的账单，为差异复核提供反例。',
                'SELECT customer, COUNT(*) AS count FROM invoices WHERE temperature > 8 GROUP BY customer ORDER BY customer',
            ),
        ]
        for name, desc, sql in samples:
            if not db.scalar(select(Sample).where(Sample.name == name)):
                db.add(Sample(name=name, description=desc, original_sql=sql))
        db.commit()
        return {
            t: db.execute(text('SELECT COUNT(*) FROM ' + t)).scalar()
            for t in (
                'users',
                'samples',
                'batches',
                'evidence',
                'invoices',
                'settlement_periods',
                'backfill_batches',
                'bill_ingestions',
            )
        }


if __name__ == '__main__':
    print(seed())
