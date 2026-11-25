<template>
  <div class="grid two">
    <!-- 左：建档 + 批次列表 -->
    <section class="stack">
      <div class="panel">
        <h2>① 带车队编号的批次请求</h2>
        <label class="field">
          <span>车队编号</span>
          <input v-model="form.fleet_no" placeholder="如 F-8" />
        </label>
        <div class="row">
          <label class="field">
            <span>站点</span>
            <select v-model="form.site_id">
              <option v-for="s in sites" :key="s.id" :value="s.id">
                {{ s.name }} — {{ s.location }}
              </option>
            </select>
          </label>
          <label class="field">
            <span>确认路线</span>
            <select v-model="form.strategy">
              <option value="GRADUAL">逐车确认（可累计进度，早确认早拿进场码）</option>
              <option value="ATOMIC">原子组合（达标后一次确认，杜绝部分成功）</option>
            </select>
          </label>
        </div>
        <label class="field">
          <span>最低就绪数（合同至少需就绪的车辆数）</span>
          <input type="number" min="1" v-model.number="form.min_ready" />
        </label>

        <h3>逐车需求（接口 / 最低目标电量 / 到站范围 / 出发时刻）</h3>
        <div v-for="(it, idx) in form.items" :key="idx" class="candidate">
          <div class="head">
            <strong>第 {{ idx + 1 }} 辆</strong>
            <button class="ghost small" @click="removeItem(idx)" :disabled="form.items.length <= 1">删除</button>
          </div>
          <div class="row">
            <label class="field">
              <span>车辆标识</span>
              <input v-model="it.vehicle_label" placeholder="如 A 车" />
            </label>
            <label class="field">
              <span>关联车辆档案（可空）</span>
              <select v-model="it.vehicle_id">
                <option :value="null">不指定（自动建专用档案）</option>
                <option v-for="v in vehicles" :key="v.id" :value="v.id">
                  V{{ v.id }} {{ v.name }} — {{ v.connector_type }}/{{ v.max_power_kw }}kW
                </option>
              </select>
            </label>
          </div>
          <div class="row">
            <label class="field">
              <span>接口</span>
              <select v-model="it.connector_type">
                <option>CCS</option>
                <option>CHAdeMO</option>
                <option>GBT</option>
              </select>
            </label>
            <label class="field">
              <span>最低目标电量 kW</span>
              <input type="number" min="1" v-model.number="it.min_power_kw" />
            </label>
            <label class="field">
              <span>粒度</span>
              <select v-model="it.mode">
                <option value="CONNECTOR">固定枪口</option>
                <option value="POOL">资源池</option>
              </select>
            </label>
          </div>
          <div class="row">
            <label class="field">
              <span>到站起</span>
              <input type="datetime-local" v-model="it.arrival_start" />
            </label>
            <label class="field">
              <span>到站止</span>
              <input type="datetime-local" v-model="it.arrival_end" />
            </label>
            <label class="field">
              <span>出发时刻</span>
              <input type="datetime-local" v-model="it.departure_at" />
            </label>
          </div>
        </div>
        <button class="secondary small" @click="addItem">+ 增加一辆车</button>
        <div style="margin-top:10px">
          <button @click="createBatch">提交批次请求</button>
        </div>
        <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">
          {{ notice.text }}
        </div>
      </div>

      <div class="panel">
        <h2>② 车队批次列表</h2>
        <table v-if="batches.length">
          <thead><tr><th>车队</th><th>路线</th><th>就绪</th><th>状态</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in batches" :key="b.id" :class="{ 'row-active': selectedId === b.id }">
              <td><strong>{{ b.fleet_no }}</strong></td>
              <td class="muted">{{ FLEET_STRATEGY_LABELS[b.strategy].split('（')[0] }}</td>
              <td class="mono">{{ b.ready_count }}/{{ b.min_ready }}</td>
              <td><span :class="['badge', b.status === 'READY' ? 'ok' : 'warn']">
                {{ FLEET_BATCH_STATUS_LABELS[b.status] }}
              </span></td>
              <td><button class="small" @click="selectBatch(b.id)">查看</button></td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无批次。</p>
      </div>
    </section>

    <!-- 右：批次详情与车队视图 -->
    <section class="stack" v-if="batch">
      <div class="panel">
        <h2>③ 车队视图：{{ batch.fleet_no }}
          <span :class="['badge', batch.status === 'READY' ? 'ok' : 'bad']" style="margin-left:8px">
            {{ FLEET_BATCH_STATUS_LABELS[batch.status] }}
          </span>
        </h2>
        <div class="kv">
          <span class="k">确认路线</span><span>{{ FLEET_STRATEGY_LABELS[batch.strategy] }}</span>
          <span class="k">最低就绪数</span><span>{{ batch.min_ready }} 辆</span>
          <span class="k">当前就绪</span><strong>{{ batch.ready_count }} 辆（还差 {{ batch.deficit }} 辆）</strong>
          <span class="k">就绪通知</span>
          <span>
            <span v-if="batch.ready_declared" class="badge ok">已宣布车队就绪 {{ fmt(batch.ready_at) }}</span>
            <span v-else class="badge muted">未宣布（单车确认不等于车队就绪）</span>
          </span>
        </div>

        <div v-if="batch.unmet_items.length" style="margin-top:12px">
          <h3>未满足车辆及原因</h3>
          <table>
            <thead><tr><th>车辆</th><th>状态</th><th>原因</th></tr></thead>
            <tbody>
              <tr v-for="u in batch.unmet_items" :key="u.item_id">
                <td>{{ u.vehicle_label }}</td>
                <td><span class="badge warn">{{ FLEET_ITEM_STATUS_LABELS[u.status] || u.status }}</span></td>
                <td class="muted" style="max-width:380px">{{ prettyReason(u.reason) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div style="margin-top:14px" v-if="batch.strategy === 'ATOMIC'">
          <button @click="atomicSolve">原子求解兼容组合并一次确认</button>
          <p class="muted" style="margin:6px 0 0">
            达标才整体落库；达不到最低就绪数时整组回滚、零容量占用，所有司机等完整组合算出后才收到结果。
          </p>
        </div>
      </div>

      <div class="panel">
        <h2>④ 逐车子预约（可追溯）</h2>
        <table>
          <thead>
            <tr><th>#</th><th>车辆/需求</th><th>到站范围 → 出发</th><th>状态/子预约</th><th>通知与操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="it in batch.items" :key="it.id">
              <td>{{ it.position }}</td>
              <td>
                <strong>{{ it.vehicle_label }}</strong><br />
                <span class="muted">{{ it.connector_type }} / ≥{{ it.min_power_kw }}kW /
                  {{ MODE_LABELS[it.mode] }}</span>
              </td>
              <td class="mono">
                {{ fmt(it.arrival_start) }}–{{ fmt(it.arrival_end) }}<br />
                出发 {{ fmt(it.departure_at) }}
              </td>
              <td>
                <span :class="['badge', itemBadge(it.status)]">{{ FLEET_ITEM_STATUS_LABELS[it.status] }}</span>
                <div v-if="it.reservation_ref" class="mono muted" style="margin-top:4px">
                  {{ it.reservation_ref }}（{{ STATUS_LABELS[it.reservation_status] }}）
                  <span v-if="it.connector_id">· 枪 {{ gunCode(it.connector_id) }}</span>
                </div>
                <div v-if="it.last_reason" class="muted" style="max-width:260px;margin-top:4px">
                  {{ prettyReason(it.last_reason) }}
                </div>
              </td>
              <td>
                <div v-if="it.access_code" class="pool-hero" style="padding:8px 10px;margin-bottom:6px">
                  进场码 <strong>{{ it.access_code }}</strong><br />
                  <span class="muted" style="font-size:12px">{{ it.navigation }}</span>
                </div>
                <div class="chip-row">
                  <button v-if="batch.strategy === 'GRADUAL' && it.status !== 'CONFIRMED'"
                          class="small" @click="confirmOne(it)">确认该车可用时段</button>
                  <button v-if="it.reservation_id && it.reservation_status === 'CONFIRMED'"
                          class="small ghost" @click="cancelSub(it)">取消子预约</button>
                  <button class="small secondary" @click="openReplan(it)">临时换车</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel" v-if="replan.item_id">
        <h2>⑤ 临时换车：{{ replan.label }}</h2>
        <div class="row">
          <label class="field">
            <span>替换为已有车辆</span>
            <select v-model="replan.vehicle_id">
              <option :value="null">不换档案，仅改下方需求（限专用车辆）</option>
              <option v-for="v in vehicles" :key="v.id" :value="v.id">
                V{{ v.id }} {{ v.name }} — {{ v.connector_type }}/{{ v.max_power_kw }}kW
              </option>
            </select>
          </label>
          <label class="field">
            <span>新接口</span>
            <select v-model="replan.connector_type">
              <option :value="null">不变</option>
              <option>CCS</option><option>CHAdeMO</option><option>GBT</option>
            </select>
          </label>
          <label class="field">
            <span>新最低功率 kW</span>
            <input type="number" v-model.number="replan.min_power_kw" />
          </label>
        </div>
        <div class="row">
          <button @click="submitReplan">提交换车并立即重算</button>
          <button class="ghost" @click="replan.item_id = null">收起</button>
        </div>
      </div>

      <div class="panel">
        <h2>⑥ 批次事件流（司机通知 / 就绪宣布 / 撤回留痕）</h2>
        <ul class="timeline">
          <li v-for="e in batch.events" :key="e.id">
            <strong :class="eventClass(e.event_type)">{{ eventLabel(e.event_type) }}</strong>
            <div class="muted mono">{{ fmt(e.created_at) }}</div>
            <pre class="mono" style="white-space:pre-wrap;margin:2px 0 0">{{ pretty(e.detail) }}</pre>
          </li>
        </ul>
      </div>
    </section>

    <section class="panel" v-else>
      <p class="muted">从左侧选择一个批次查看车队视图，或先提交新的批次请求。</p>
    </section>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import {
  api, FLEET_BATCH_STATUS_LABELS, FLEET_ITEM_STATUS_LABELS, FLEET_STRATEGY_LABELS,
  MODE_LABELS, newIdempotencyKey, STATUS_LABELS,
} from '../api.js'

const sites = ref([])
const vehicles = ref([])
const batches = ref([])
const selectedId = ref(null)
const batch = ref(null)
const notice = ref(null)

function blankItem() {
  return {
    vehicle_label: '',
    vehicle_id: null,
    connector_type: 'CCS',
    min_power_kw: 120,
    accessible_required: false,
    arrival_start: '2026-11-27T05:00',
    arrival_end: '2026-11-27T05:30',
    departure_at: '2026-11-27T06:00',
    mode: 'CONNECTOR',
  }
}

const form = reactive({
  fleet_no: 'F-14',
  site_id: 2,
  strategy: 'GRADUAL',
  min_ready: 2,
  items: [blankItem(), blankItem(), blankItem()],
})

const replan = reactive({ item_id: null, label: '', vehicle_id: null, connector_type: null, min_power_kw: null })

onMounted(loadAll)

async function loadAll() {
  sites.value = await api.get('/api/sites')
  vehicles.value = await api.get('/api/vehicles')
  batches.value = await api.get('/api/fleet/batches')
  if (selectedId.value) await selectBatch(selectedId.value)
}

function addItem() { form.items.push(blankItem()) }
function removeItem(i) { form.items.splice(i, 1) }

function toApiItem(it) {
  return {
    vehicle_label: it.vehicle_label || `车辆 ${Math.round(Math.random() * 1000)}`,
    vehicle_id: it.vehicle_id,
    connector_type: it.connector_type,
    min_power_kw: it.min_power_kw,
    accessible_required: it.accessible_required || false,
    arrival_start: it.arrival_start + ':00',
    arrival_end: it.arrival_end + ':00',
    departure_at: it.departure_at + ':00',
    mode: it.mode,
  }
}

async function createBatch() {
  notice.value = null
  try {
    const body = {
      fleet_no: form.fleet_no,
      site_id: form.site_id,
      strategy: form.strategy,
      min_ready: form.min_ready,
      items: form.items.map(toApiItem),
      idempotency_key: newIdempotencyKey(),
    }
    const created = await api.post('/api/fleet/batches', body)
    notice.value = { kind: 'ok', text: `批次 ${created.fleet_no} 已建档（${created.strategy}，最低就绪 ${created.min_ready} 辆）` }
    await loadAll()
    selectBatch(created.id)
  } catch (e) {
    notice.value = { kind: 'bad', text: '建档被拒：' + e.message }
  }
}

async function selectBatch(id) {
  selectedId.value = id
  batch.value = await api.get(`/api/fleet/batches/${id}`)
}

async function confirmOne(it) {
  notice.value = null
  try {
    const r = await api.post(`/api/fleet/batches/${batch.value.id}/items/${it.id}/confirm`, {
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = {
      kind: r.outcome === 'CONFIRMED' ? 'ok' : 'bad',
      text: r.message,
    }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function atomicSolve() {
  notice.value = null
  try {
    const r = await api.post(`/api/fleet/batches/${batch.value.id}/atomic-confirm`, {
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = {
      kind: r.outcome === 'CONFIRMED' ? 'ok' : 'bad',
      text: r.message,
    }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function cancelSub(it) {
  notice.value = null
  try {
    await api.post(`/api/reservations/${it.reservation_id}/cancel`, {
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'info', text: `${it.vehicle_label} 的子预约已取消，批次已立即重算` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

function openReplan(it) {
  replan.item_id = it.id
  replan.label = it.vehicle_label
  replan.vehicle_id = null
  replan.connector_type = null
  replan.min_power_kw = null
}

async function submitReplan() {
  notice.value = null
  try {
    const body = { idempotency_key: newIdempotencyKey() }
    if (replan.vehicle_id) body.vehicle_id = replan.vehicle_id
    if (replan.connector_type) body.connector_type = replan.connector_type
    if (replan.min_power_kw) body.min_power_kw = replan.min_power_kw
    const r = await api.post(
      `/api/fleet/batches/${batch.value.id}/items/${replan.item_id}/replan`, body,
    )
    notice.value = { kind: 'info', text: '换车完成，旧子预约已作废，批次已重算：' + r.status }
    replan.item_id = null
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

function gunCode(id) {
  for (const s of sites.value) {
    const c = s.connectors.find((x) => x.id === id)
    if (c) return c.code
  }
  return '#' + id
}
function fmt(t) { return t ? t.replace('T', ' ').slice(5, 16) : '' }
function pretty(s) {
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}
function prettyReason(s) {
  try {
    const o = JSON.parse(s)
    if (typeof o === 'object' && o.message) {
      return o.message + (o.connector_reasons ? '；' + o.connector_reasons.map((c) =>
        `${c.connector_code}: ${c.reasons.join('，')}`).join('；') : '')
    }
    return JSON.stringify(o)
  } catch { return s }
}
function itemBadge(s) {
  return {
    CONFIRMED: 'ok', PENDING: 'muted', FAILED: 'bad',
    CANCELLED: 'muted', RESELECTED: 'warn',
  }[s] || 'muted'
}
function eventLabel(t) {
  return {
    BATCH_CREATED: '批次建档',
    ITEM_CONFIRMED: '子预约确认',
    ITEM_CONFIRM_FAILED: '子预约被拒',
    ITEM_CANCELLED: '子预约取消',
    ITEM_RESELECTION: '枪口失能·改选',
    ITEM_REPLANNED: '临时换车',
    DRIVER_NOTIFY: '司机通知（导航/进场码）',
    FLEET_READY: '车队就绪宣布',
    FLEET_READY_WITHDRAWN: '车队就绪撤回',
    ATOMIC_SOLVE_SUCCEEDED: '原子组合成功',
    ATOMIC_SOLVE_FAILED: '原子组合失败（零占用）',
  }[t] || t
}
function eventClass(t) {
  if (t === 'FLEET_READY') return 'badge ok'
  if (t === 'FLEET_READY_WITHDRAWN' || t.endsWith('FAILED') || t === 'ITEM_RESELECTION') return 'badge bad'
  if (t === 'DRIVER_NOTIFY') return 'badge info'
  return 'badge muted'
}
</script>

<style scoped>
.row-active { background: var(--brand-soft); }
</style>
