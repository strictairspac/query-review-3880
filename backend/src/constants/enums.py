ROLES = ('admin', 'operator', 'viewer')

# 复核批次状态。
# waiting_watermark: 比对已完成但账期水位未稳定，不发布通过。
# backfilling: 证据因迟到账单失效，正在补算受影响客户。
STATUSES = ('queued', 'running', 'waiting_watermark', 'passed', 'different', 'failed', 'backfilling')

FINAL_STATUSES = ('passed', 'different', 'failed')

PERIOD_STATUSES = ('open', 'closed')

# 水位状态：stable=完成、waiting=待等水位、backfilling=补算中。
WATERMARK_STATES = ('stable', 'waiting', 'backfilling')

BACKFILL_STATUSES = ('queued', 'running', 'interrupted', 'done', 'failed')
BACKFILL_ACTIVE_STATUSES = ('queued', 'running', 'interrupted')

EVIDENCE_STATES = ('valid', 'stale', 'failed')
