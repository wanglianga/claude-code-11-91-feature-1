async function request(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // FastAPI 的 detail 可能是字符串或结构化对象
    const detail = data.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || JSON.stringify(detail || data)
    const err = new Error(message)
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
}

export function newIdempotencyKey() {
  // 前端生成可读幂等键；重复提交时复用同一个键即可回放原结果
  return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8)
}

export const STATUS_LABELS = {
  CONFIRMED: '已确认',
  ARRIVED: '已到站',
  CHARGING: '充电中（受保护）',
  COMPLETED: '已完成',
  CANCELLED: '已取消（已释放）',
  NO_SHOW: '迟到失约（已释放）',
  NEEDS_RESELECTION: '需改选',
  MANUAL_CONTACT: '需人工联系',
}

export const MODE_LABELS = {
  CONNECTOR: '固定枪口',
  POOL: '资源池',
}

export const IMPACT_LABELS = {
  UNCHANGED: '继续履约',
  NEEDS_RESELECTION: '进入改选',
  MANUAL_CONTACT: '人工联系',
  PROTECTED_CHARGING: '充电中受保护',
}

export const FLEET_STRATEGY_LABELS = {
  GRADUAL: '逐车确认（早确认早拿进场码）',
  ATOMIC: '原子组合（达标后一次确认）',
}

export const FLEET_BATCH_STATUS_LABELS = {
  OPEN: '未达最低就绪数',
  READY: '车队已就绪',
}

export const FLEET_ITEM_STATUS_LABELS = {
  PENDING: '待确认',
  CONFIRMED: '子预约已确认',
  FAILED: '确认被拒',
  CANCELLED: '子预约已取消',
  RESELECTED: '枪口失能待改选',
}
