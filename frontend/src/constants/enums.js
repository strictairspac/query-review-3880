export const ROLES={admin:'管理员',operator:'操作员',viewer:'观察员'};
export const STATUSES={queued:'排队中',running:'执行中',waiting_watermark:'待等水位',passed:'结果一致',different:'存在差异',failed:'执行失败',backfilling:'补算中'};
export const WATERMARK_LABELS={stable:'完成',waiting:'待等水位',backfilling:'补算'};
export const EVIDENCE_STATES={valid:'可查',stale:'部分失效·补算中',failed:'无结果'};
export const BACKFILL_STATUSES={queued:'排队中',running:'执行中',interrupted:'中断待恢复',done:'已完成',failed:'补算失败'};
export const PERIOD_STATUSES={open:'开账中',closed:'已关账'};
