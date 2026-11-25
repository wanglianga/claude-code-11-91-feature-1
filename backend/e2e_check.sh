#!/usr/bin/env bash
# 题面场景手工 E2E：针对运行中的 API（默认 127.0.0.1:8000）逐步验证。
# 前置：数据库已重置为种子状态（python -m app.seed）。
# 用法：bash e2e_check.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"
F="2026-11-27T18"
pass=0; fail=0

check() { # desc expected actual
  if [ "$2" = "$3" ]; then echo "  ✅ $1 ($3)"; pass=$((pass+1));
  else echo "  ❌ $1：期望 [$2] 实际 [$3]"; fail=$((fail+1)); fi
}
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
code() { curl -s -o /tmp/e2e_body.json -w '%{http_code}' -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 0. 健康检查 =="
check "health" "ok" "$(curl -s $BASE/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 1. 候选搜索（18:00-18:10，90kW，固定枪口）：A 占用 / B 唯一合格 / C 功率不足 / D 接口不符 =="
post /api/search "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\"}" | python3 -c '
import json,sys
d=json.load(sys.stdin); by={c["connector_code"]:c for c in d["connector_candidates"]}
assert by["A"]["eligible"] is False and any("R-SEEDA01" in r for r in by["A"]["reasons"])
assert by["B"]["eligible"] is True
assert by["C"]["eligible"] is False and any("低于" in r for r in by["C"]["reasons"])
assert by["D"]["eligible"] is False and any("接口不兼容" in r for r in by["D"]["reasons"])
print("  ✅ 四枪判定与限制原因齐备")
' || fail=$((fail+1))

echo "== 2. 资源池搜索：池内只剩 1 个可确认名额 =="
check "池内可确认名额" "1" "$(post /api/search "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["confirmable_slots"])')"

echo "== 3. 并发抢 B（固定枪口）：一笔 200 锁定 B，一笔 409 =="
rm -f /tmp/c1.json /tmp/c2.json /tmp/c1.code /tmp/c2.code
curl -s -o /tmp/c1.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\",\"idempotency_key\":\"e2e-conn-aaaa-1\"}" > /tmp/c1.code &
curl -s -o /tmp/c2.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\",\"idempotency_key\":\"e2e-conn-bbbb-2\"}" > /tmp/c2.code &
wait
check "并发结果码集合" "200,409," "$(sort /tmp/c1.code /tmp/c2.code | tr '\n' ',')"
check "胜出订单锁定 B 枪" "2" "$(python3 -c '
import json
if open("/tmp/c1.code").read()=="200": w=json.load(open("/tmp/c1.json"))
else: w=json.load(open("/tmp/c2.json"))
print(w["connector_id"])')"

echo "== 4. 幂等：同键重放只返回原订单（放在 20:00 避免与后续场景交叉）=="
R1=$(post /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"2026-11-27T20:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-idem-aaaa-1\"}")
REF1=$(echo "$R1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ref"])')
R2=$(post /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"2026-11-27T20:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-idem-aaaa-1\"}")
check "重放返回同一 ref" "$REF1" "$(echo "$R2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ref"])')"

echo "== 5. 18:10 起 B 降额 60kW：18:10 已结束的预约不在影响清单 =="
SHORT=$(post /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":60,\"idempotency_key\":\"e2e-short-aaaa-1\"}")
SHORT_ID=$(echo "$SHORT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
DERATE=$(post /api/operations/maintenance "{\"connector_id\":2,\"kind\":\"DERATE\",\"start_at\":\"$F:10:00\",\"planned_end_at\":\"2026-11-27T19:00:00\",\"effective_power_kw\":60,\"reason\":\"模块高温\",\"idempotency_key\":\"e2e-maint-aaaa-1\"}")
check "短单与降额无交集、未被撤销" "0" "$(echo "$DERATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(sum(1 for i in d['impacts'] if i['reservation_id']==$SHORT_ID))")"
check "短单状态仍为 CONFIRMED" "CONFIRMED" "$(curl -s $BASE/api/reservations/$SHORT_ID | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 6. 降额后：18:15 的 90kW 被拒，60kW 仍可使用 B =="
check "90kW 请求被拒" "409" "$(code /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:15:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-deny-aaaa-1\"}")"
check "60kW 请求成功" "200" "$(code /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:15:00\",\"duration_minutes\":10,\"min_power_kw\":60,\"idempotency_key\":\"e2e-ok60-aaaa-1\"}")"
check "60kW 订单分配到 B 枪" "2" "$(python3 -c 'import json;print(json.load(open("/tmp/e2e_body.json"))["connector_id"])')"

echo "== 7. 池模式并发（18:30 后仅 A 满足 90kW，池内恰剩 1 名额）：一赢一拒且不指定枪号 =="
rm -f /tmp/p1.json /tmp/p2.json /tmp/p1.code /tmp/p2.code
curl -s -o /tmp/p1.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:30:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\",\"idempotency_key\":\"e2e-pool-aaaa-1\"}" > /tmp/p1.code &
curl -s -o /tmp/p2.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:30:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\",\"idempotency_key\":\"e2e-pool-bbbb-2\"}" > /tmp/p2.code &
wait
check "池并发结果码集合" "200,409," "$(sort /tmp/p1.code /tmp/p2.code | tr '\n' ',')"
check "池赢家暂不指定枪号" "None" "$(python3 -c '
import json
w=json.load(open("/tmp/p1.json")) if open("/tmp/p1.code").read()=="200" else json.load(open("/tmp/p2.json"))
print(w["connector_id"])')"

echo "== 8. 开始充电后故障：会话受保护，取消被拒，维护判定为 PROTECTED_CHARGING =="
CHG=$(post /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:40:00\",\"duration_minutes\":20,\"min_power_kw\":60,\"preferred_connector_id\":3,\"idempotency_key\":\"e2e-chg-aaaa-1\"}")
CID=$(echo "$CHG" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
check "充电前固定锁定 C 枪" "3" "$(echo "$CHG" | python3 -c 'import json,sys;print(json.load(sys.stdin)["connector_id"])')"
post "/api/reservations/$CID/arrive" '{"idempotency_key":"e2e-chg-bbbb-2"}' > /dev/null
post "/api/reservations/$CID/start" '{"idempotency_key":"e2e-chg-cccc-3"}' > /dev/null
IMP=$(post /api/operations/maintenance "{\"connector_id\":3,\"kind\":\"OUTAGE\",\"start_at\":\"$F:45:00\",\"planned_end_at\":\"$F:50:00\",\"reason\":\"枪口故障\",\"idempotency_key\":\"e2e-chg-dddd-4\"}")
check "充电单判定受保护" "PROTECTED_CHARGING" "$(echo "$IMP" | python3 -c "import json,sys;print(next(i['decision'] for i in json.load(sys.stdin)['impacts'] if i['reservation_id']==$CID))")"
check "充电中取消被拒" "409" "$(code "/api/reservations/$CID/cancel" '{"idempotency_key":"e2e-chg-eeee-5"}')"
check "订单状态仍是 CHARGING" "CHARGING" "$(curl -s $BASE/api/reservations/$CID | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 9. 车队批次 F-8（逐车确认，最低就绪数 2）：单车确认不等于车队就绪 =="
# A 车确认成功：只计 1 辆，批次仍 OPEN，不发车队就绪通知
A=$(post /api/fleet/batches/1/items/1/confirm '{"idempotency_key":"e2e-f8-a-000001"}')
check "A 车确认 outcome" "CONFIRMED" "$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
check "A 确认后就绪数" "1" "$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ready_count"])')"
check "A 确认后批次状态" "OPEN" "$(echo "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
check "未发车队就绪通知" "False" "$(echo "$A" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(any(e["event_type"]=="FLEET_READY" for e in d["events"]))')"
check "A 已收到进场码" "True" "$(echo "$A" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(bool(next(i for i in d["items"] if i["id"]==1)["access_code"]))')"
# 同键重放：不产生新子预约
A_REF=$(echo "$A" | python3 -c 'import json,sys;print(next(i for i in json.load(sys.stdin)["items"] if i["id"]==1)["reservation_ref"])')
check "A 重放返回同一子预约" "$A_REF" "$(post /api/fleet/batches/1/items/1/confirm '{"idempotency_key":"e2e-f8-a-000001"}' | python3 -c 'import json,sys;print(next(i for i in json.load(sys.stdin)["items"] if i["id"]==1)["reservation_ref"])')"

echo "== 10. N1 唯一 120kW 被占、N3 被个人用户占用：B/C 均被拒，界面保持未达 2 辆 =="
check "B 车确认被拒" "REJECTED" "$(post /api/fleet/batches/1/items/2/confirm '{"idempotency_key":"e2e-f8-b-000001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
check "C 车确认被拒（N3 占用）" "REJECTED" "$(post /api/fleet/batches/1/items/3/confirm '{"idempotency_key":"e2e-f8-c-000001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
check "批次仍未宣布就绪" "False" "$(curl -s $BASE/api/fleet/batches/1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["ready_declared"])')"
check "未满足清单含 B/C 两车" "2" "$(curl -s $BASE/api/fleet/batches/1 | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["unmet_items"]))')"

echo "== 11. 个人用户释放 N3 后 C 可确认：达到 2 辆才发车队就绪通知 =="
N3_ID=$(curl -s $BASE/api/reservations | python3 -c 'import json,sys;print(next(r["id"] for r in json.load(sys.stdin) if r["ref"]=="R-SEEDN01"))')
post "/api/reservations/$N3_ID/cancel" '{"idempotency_key":"e2e-f8-rel-00001"}' > /dev/null
C2=$(post /api/fleet/batches/1/items/3/confirm '{"idempotency_key":"e2e-f8-c-000002"}')
check "C 车二次确认成功" "CONFIRMED" "$(echo "$C2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
check "就绪数达到 2" "2" "$(echo "$C2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ready_count"])')"
check "批次状态 READY" "READY" "$(echo "$C2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
check "已发车队就绪通知" "True" "$(echo "$C2" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(any(e["event_type"]=="FLEET_READY" for e in d["events"]))')"

echo "== 12. 一笔取消使已宣布的车队就绪重新变为不足 =="
A_RID=$(echo "$C2" | python3 -c 'import json,sys;print(next(i for i in json.load(sys.stdin)["items"] if i["id"]==1)["reservation_id"])')
post "/api/reservations/$A_RID/cancel" '{"idempotency_key":"e2e-f8-cancela-1"}' > /dev/null
check "取消后就绪数跌回 1" "1" "$(curl -s $BASE/api/fleet/batches/1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["ready_count"])')"
check "批次回到 OPEN" "OPEN" "$(curl -s $BASE/api/fleet/batches/1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
check "产生就绪撤回事件" "True" "$(curl -s $BASE/api/fleet/batches/1 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(any(e["event_type"]=="FLEET_READY_WITHDRAWN" for e in d["events"]))')"

echo "== 13. 原子组合路线：达不到最低就绪数时整体拒绝、零容量占用 =="
BEFORE=$(curl -s $BASE/api/reservations | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
ATOMIC_ID=$(post /api/fleet/batches '{"fleet_no":"F-9E2E","site_id":2,"strategy":"ATOMIC","min_ready":3,"items":[{"vehicle_label":"A 车","vehicle_id":6,"connector_type":"CCS","min_power_kw":120,"arrival_start":"2026-11-27T05:00:00","arrival_end":"2026-11-27T05:30:00","departure_at":"2026-11-27T06:00:00","mode":"CONNECTOR"},{"vehicle_label":"B 车","vehicle_id":7,"connector_type":"CCS","min_power_kw":120,"arrival_start":"2026-11-27T05:00:00","arrival_end":"2026-11-27T05:30:00","departure_at":"2026-11-27T06:00:00","mode":"CONNECTOR"},{"vehicle_label":"C 车","vehicle_id":8,"connector_type":"CCS","min_power_kw":60,"arrival_start":"2026-11-27T05:00:00","arrival_end":"2026-11-27T06:00:00","departure_at":"2026-11-27T06:00:00","mode":"CONNECTOR"}],"idempotency_key":"e2e-f9-create-001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
check "原子求解被整体拒绝" "REJECTED" "$(post /api/fleet/batches/$ATOMIC_ID/atomic-confirm '{"idempotency_key":"e2e-f9-solve-001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
check "原子失败零容量占用" "$BEFORE" "$(curl -s $BASE/api/reservations | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
check "原子失败重放仍零占用" "REJECTED" "$(post /api/fleet/batches/$ATOMIC_ID/atomic-confirm '{"idempotency_key":"e2e-f9-solve-001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"

echo
echo "通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ]
