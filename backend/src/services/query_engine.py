"""Strict read-only bounded SQL execution against a single seeded business table."""
import hashlib
import json
import time
from collections import Counter

import sqlglot
from sqlglot import exp
from sqlalchemy import text

from ..config.database import engine

ALLOWED_FUNCTIONS = {'SUM', 'COUNT', 'AVG', 'MIN', 'MAX', 'ROUND', 'COALESCE', 'CAST'}
ALLOWED_COLUMNS = {'customer', 'route', 'amount', 'temperature', 'invoice_id', 'status'}
# 内部注入的列，用户 SQL 不可直接引用。
SCOPE_COLUMN = 'settlement_period'
INGEST_COLUMN = 'ingest_seq'


def validate_sql(sql):
    try:
        trees = sqlglot.parse(sql, read='postgres')
    except Exception as exc:
        raise ValueError('SQL 无法解析：' + str(exc)[:180])
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError('只允许单条 SELECT')
    tree = trees[0]
    if tree.args.get('into') or tree.args.get('locks') or tree.find(exp.With):
        raise ValueError('不允许 INTO、锁或 CTE')
    tables = list(tree.find_all(exp.Table))
    if not tables or any(t.name != 'invoices' or t.db or t.catalog for t in tables):
        raise ValueError('只允许访问 invoices 表')
    for col in tree.find_all(exp.Column):
        if col.name not in ALLOWED_COLUMNS and col.name != '*':
            raise ValueError('未知业务字段：' + col.name)
    for f in tree.find_all(exp.Func):
        if f.sql_name() not in ALLOWED_FUNCTIONS:
            raise ValueError('不允许函数：' + f.sql_name())
    if len(list(tree.walk())) > 250:
        raise ValueError('SQL 结构过大')
    return tree.sql(dialect='postgres')


def scope_sql(sql, period=None, watermark=None, customers=None):
    """把每处 invoices 引用替换为带账期/水位/客户条件的子查询。

    不能只在最外层追加 WHERE：样本 SQL 可能含相关子查询，那样内层仍会读到
    其他账期或水位之后的迟到账单，导致账期隔离失效。逐表替换可保证内外一致。
    """
    tree = sqlglot.parse_one(sql, read='postgres')
    conditions = []
    if period is not None:
        conditions.append(
            exp.EQ(this=exp.column(SCOPE_COLUMN), expression=exp.Literal.string(period))
        )
    if watermark is not None:
        conditions.append(
            exp.Paren(
                this=exp.Or(
                    expressions=[
                        exp.Is(this=exp.column(INGEST_COLUMN), expression=exp.Null()),
                        exp.LTE(this=exp.column(INGEST_COLUMN), expression=exp.Literal.number(int(watermark))),
                    ]
                )
            )
        )
    if customers is not None:
        if not customers:
            conditions.append(exp.false())
        else:
            conditions.append(
                exp.Paren(
                    this=exp.Or(
                        expressions=[
                            exp.EQ(this=exp.column('customer'), expression=exp.Literal.string(c)) for c in customers
                        ]
                    )
                )
            )
    if not conditions:
        return tree.sql(dialect='postgres')
    scope = conditions[0]
    for condition in conditions[1:]:
        scope = exp.And(this=scope, expression=condition)
    for table in list(tree.find_all(exp.Table)):
        if table.name != 'invoices':
            continue
        alias = table.alias or 'invoices'
        inner = (
            exp.select(exp.Star())
            .from_(exp.Table(this=exp.to_identifier('invoices')))
            .where(scope.copy())
        )
        replacement = exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))
        table.replace(replacement)
    return tree.sql(dialect='postgres')


def _execute(sql):
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text('SET TRANSACTION READ ONLY'))
            conn.execute(text("SET LOCAL statement_timeout='2500ms'"))
            conn.execute(text('SET LOCAL ROLE query_reader'))
            plan = conn.execute(text('EXPLAIN (ANALYZE, FORMAT JSON, BUFFERS) ' + sql)).scalar()
            started = time.perf_counter()
            cursor = conn.execute(text(sql))
            columns = list(cursor.keys())
            rows = cursor.fetchmany(1001)
            if len(rows) > 1000:
                raise ValueError('结果超过 1000 行，请聚合或 LIMIT')
            rows = [[str(v) if v is not None else None for v in row] for row in rows]
            return {
                'columns': columns,
                'rows': rows,
                'row_count': len(rows),
                'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
                'plan': plan,
            }


def execute_query(sql, period=None, watermark=None, customers=None):
    validated = validate_sql(sql)
    return _execute(scope_sql(validated, period=period, watermark=watermark, customers=customers))


def canonical(data):
    return Counter(json.dumps(row, ensure_ascii=False) for row in data['rows'])


def checksum(data):
    return hashlib.sha256(json.dumps(sorted(canonical(data).items()), ensure_ascii=False).encode()).hexdigest()


def summarize(left, right):
    """两侧结果的多重集合比较摘要。"""
    a, b = canonical(left), canonical(right)
    equivalent = len(left['columns']) == len(right['columns']) and a == b
    return {
        'equivalent': equivalent,
        'comparison': '忽略行顺序、保留重复行；按输出列位置比较，数字按数据库文本表示',
        'original': left,
        'candidate': right,
        'original_checksum': checksum(left),
        'candidate_checksum': checksum(right),
        'missing_rows': list((a - b).elements())[:10],
        'extra_rows': list((b - a).elements())[:10],
        'note': '执行时间受缓存与机器负载影响；不据一次耗时断言优化收益',
    }


def compare(original, candidate, period=None, watermark=None, customers=None):
    snapshot = {
        'period': period,
        'watermark_seq': watermark,
        'customers': list(customers) if customers is not None else None,
        'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    left = execute_query(original, period=period, watermark=watermark, customers=customers)
    right = execute_query(candidate, period=period, watermark=watermark, customers=customers)
    result = summarize(left, right)
    result['scope'] = snapshot
    return result
