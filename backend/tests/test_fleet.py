"""车队批次场景测试：F-8 样例、两条确认路线、重算/回退、换车与幂等。

种子固定 id（见 app/seed.py）：
- 北站 site_id=2；枪口 N1=5（120kW，05:30 起停用）、N2=6（120kW，05:30 前停用）、
  N3=7（60kW，06:00 起停用）；个人预约 R-SEEDN01 占用 N3 的 05:00-06:00。
- 车辆 V5=5（个人用户）、V6=6（F-8 A 车 120kW）、V7=7（B 车 120kW）、V8=8（C 车 60kW）。
- 预置批次 F-8：id=1，GRADUAL，min_ready=2，items id=1/2/3。
"""

from tests.conftest import dt

NORTH = 2
GUN_N1, GUN_N2, GUN_N3 = 5, 6, 7
V5, V6, V7, V8 = 5, 6, 7, 8
F8_BATCH = 1


def _fleet_item_body(label, vid, power, arr_end, depart="06:00", arr_start="05:00"):
    return {
        "vehicle_label": label,
        "vehicle_id": vid,
        "connector_type": "CCS",
        "min_power_kw": power,
        "accessible_required": False,
        "arrival_start": dt(arr_start),
        "arrival_end": dt(arr_end),
        "departure_at": dt(depart),
        "mode": "CONNECTOR",
    }


def _create_batch(client, fleet_no, strategy, min_ready, items, key):
    return client.post("/api/fleet/batches", json={
        "fleet_no": fleet_no,
        "site_id": NORTH,
        "strategy": strategy,
        "min_ready": min_ready,
        "items": items,
        "idempotency_key": key,
    })


def _confirm_item(client, batch_id, item_id, key, preferred=None):
    body = {"idempotency_key": key}
    if preferred is not None:
        body["preferred_connector_id"] = preferred
    return client.post(f"/api/fleet/batches/{batch_id}/items/{item_id}/confirm", json=body)


def _cancel_ref(client, ref, key):
    rid = next(r["id"] for r in client.get("/api/reservations").json() if r["ref"] == ref)
    resp = client.post(f"/api/reservations/{rid}/cancel", json={"idempotency_key": key})
    return rid, resp


def _event_types(batch):
    return [e["event_type"] for e in batch["events"]]


# ---------- 1. 种子：F-8 批次初始为未就绪 ----------

def test_seed_f8_batch_starts_open(client):
    b = client.get(f"/api/fleet/batches/{F8_BATCH}").json()
    assert b["fleet_no"] == "F-8"
    assert b["strategy"] == "GRADUAL" and b["min_ready"] == 2
    assert b["status"] == "OPEN" and b["ready_count"] == 0
    assert b["ready_declared"] is False and b["deficit"] == 2
    assert [i["status"] for i in b["items"]] == ["PENDING"] * 3
    assert {u["vehicle_label"] for u in b["unmet_items"]} == {
        "A 车", "B 车", "C 车（备用）",
    }


# ---------- 2. F-8 主叙事：占用时最多就绪 1 辆，释放后达标，取消再回退 ----------

def test_f8_gradual_partial_release_ready_withdraw(client):
    # A 车确认成功：单车就绪但车队未达标，不能发车队就绪通知
    a = _confirm_item(client, F8_BATCH, 1, "key-f8-a-000001")
    assert a.status_code == 200
    body = a.json()
    assert body["outcome"] == "CONFIRMED"
    assert body["ready_count"] == 1 and body["status"] == "OPEN"
    assert body["ready_declared"] is False
    assert "FLEET_READY" not in _event_types(body)
    a_item = next(i for i in body["items"] if i["id"] == 1)
    assert a_item["access_code"] and a_item["navigation"]  # 早确认先拿导航/进场码

    # B 车（120kW）：05:00-05:30 唯一 120kW 枪 N1 已被 A 锁定，确认被拒
    b = _confirm_item(client, F8_BATCH, 2, "key-f8-b-000001").json()
    assert b["outcome"] == "REJECTED"
    assert b["ready_count"] == 1 and b["status"] == "OPEN"
    b_item = next(i for i in b["items"] if i["id"] == 2)
    assert b_item["status"] == "FAILED" and "没有可确认的合格枪口" in b_item["last_reason"]
    assert "FLEET_READY" not in _event_types(b)

    # C 车（60kW）：N3 被个人用户占用，同样被拒，界面保持“未达 2 辆”
    c = _confirm_item(client, F8_BATCH, 3, "key-f8-c-000001").json()
    assert c["outcome"] == "REJECTED" and c["ready_declared"] is False
    assert c["deficit"] == 1

    # 个人用户释放 N3
    _, cancel_resp = _cancel_ref(client, "R-SEEDN01", "key-f8-rel-00001")
    assert cancel_resp.json()["status"] == "CANCELLED"

    # C 车重新确认成功 -> 达到最低就绪数 2，发车队就绪通知
    c2 = _confirm_item(client, F8_BATCH, 3, "key-f8-c-000002").json()
    assert c2["outcome"] == "CONFIRMED"
    assert c2["ready_count"] == 2 and c2["status"] == "READY"
    assert c2["ready_declared"] is True and c2["ready_at"] is not None
    assert "FLEET_READY" in _event_types(c2)
    c_item = next(i for i in c2["items"] if i["id"] == 3)
    assert c_item["access_code"]  # C 也拿到进场码

    # A 的子预约被取消：就绪数跌回 1，已宣布的车队就绪必须撤回
    a_rid = next(i for i in c2["items"] if i["id"] == 1)["reservation_id"]
    client.post(f"/api/reservations/{a_rid}/cancel", json={"idempotency_key": "key-f8-cancela-1"})
    final = client.get(f"/api/fleet/batches/{F8_BATCH}").json()
    assert final["ready_count"] == 1 and final["status"] == "OPEN"
    assert final["ready_declared"] is False and final["ready_at"] is None
    assert "FLEET_READY_WITHDRAWN" in _event_types(final)
    withdrawn = next(e for e in final["events"] if e["event_type"] == "FLEET_READY_WITHDRAWN")
    assert '"deficit": 1' in withdrawn["detail"]


# ---------- 3. 单车确认不能伪装成车队就绪 ----------

def test_single_vehicle_confirmation_never_declares_fleet_ready(client):
    body = _confirm_item(client, F8_BATCH, 1, "key-f8-single-01").json()
    assert body["outcome"] == "CONFIRMED"
    assert body["status"] == "OPEN" and body["ready_declared"] is False
    notices = [e for e in body["events"] if e["event_type"] == "DRIVER_NOTIFY"]
    assert len(notices) == 1  # 只有司机通知，没有车队就绪通知


# ---------- 4. 逐车确认幂等：重复提交不多占容量 ----------

def test_gradual_confirm_idempotent_no_double_capacity(client):
    first = _confirm_item(client, F8_BATCH, 1, "key-f8-idem-0001")
    assert first.status_code == 200
    ref = next(i for i in first.json()["items"] if i["id"] == 1)["reservation_id"]

    replay = _confirm_item(client, F8_BATCH, 1, "key-f8-idem-0001")
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "CONFIRMED"
    assert next(i for i in replay.json()["items"] if i["id"] == 1)["reservation_id"] == ref

    # 只多出 A 的一笔子预约（种子两笔 + 一笔）
    refs = [r["ref"] for r in client.get("/api/reservations").json()]
    assert len(refs) == 3

    # 被拒确认的同键重放也返回首次的 REJECTED，不会重新判定/占容量
    rejected = _confirm_item(client, F8_BATCH, 2, "key-f8-idem-0002")
    assert rejected.json()["outcome"] == "REJECTED"
    replay_rej = _confirm_item(client, F8_BATCH, 2, "key-f8-idem-0002")
    assert replay_rej.json()["outcome"] == "REJECTED"
    assert len(client.get("/api/reservations").json()) == 3


# ---------- 5. 原子组合：失败零占用，成功后司机同时收结果 ----------

def _atomic_batch(client, fleet_no="F-9", key="key-atomic-create-1"):
    items = [
        _fleet_item_body("A 车", V6, 120, "05:30"),
        _fleet_item_body("B 车", V7, 120, "05:30"),
        _fleet_item_body("C 车", V8, 60, "06:00"),
    ]
    resp = _create_batch(client, fleet_no, "ATOMIC", 2, items, key)
    assert resp.status_code == 200
    return resp.json()["id"]


def test_atomic_failure_rolls_back_zero_occupancy(client):
    batch_id = _atomic_batch(client)
    before = len(client.get("/api/reservations").json())

    resp = client.post(f"/api/fleet/batches/{batch_id}/atomic-confirm",
                       json={"idempotency_key": "key-atomic-solve-01"})
    # 失败以 200 + REJECTED 返回（业务结果可持久化、可重放），而不是部分成功
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "REJECTED"
    assert body["ready_count"] == 0 and body["min_ready"] == 2
    assert "整体拒绝" in body["message"]

    # 整组回滚：没有任何新子预约，批次仍 OPEN，车辆行记录失败原因
    assert len(client.get("/api/reservations").json()) == before
    batch = client.get(f"/api/fleet/batches/{batch_id}").json()
    assert batch["status"] == "OPEN" and batch["ready_count"] == 0
    assert "ATOMIC_SOLVE_FAILED" in _event_types(batch)
    assert all(i["reservation_id"] is None for i in batch["items"])
    assert len(batch["unmet_items"]) >= 1

    # 失败的原子确认同样幂等：重放返回同一个 REJECTED，仍零占用
    replay = client.post(f"/api/fleet/batches/{batch_id}/atomic-confirm",
                         json={"idempotency_key": "key-atomic-solve-01"})
    assert replay.status_code == 200 and replay.json()["outcome"] == "REJECTED"
    assert len(client.get("/api/reservations").json()) == before


def test_atomic_success_all_or_nothing_and_batched_notify(client):
    batch_id = _atomic_batch(client, fleet_no="F-10", key="key-atomic-create-2")
    _cancel_ref(client, "R-SEEDN01", "key-atomic-rel-0001")

    resp = client.post(f"/api/fleet/batches/{batch_id}/atomic-confirm",
                       json={"idempotency_key": "key-atomic-solve-02"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "READY" and body["ready_count"] == 2
    # 两笔成功子预约（A/B 中一支 120kW + C 的 60kW），备用车 FAILED 但不占容量
    confirmed = [i for i in body["items"] if i["status"] == "CONFIRMED"]
    failed = [i for i in body["items"] if i["status"] == "FAILED"]
    assert len(confirmed) == 2 and len(failed) == 1
    # 所有司机通知在同一次求解后统一签发
    notifies = [e for e in body["events"] if e["event_type"] == "DRIVER_NOTIFY"]
    assert len(notifies) == 2
    assert all(i["access_code"] for i in confirmed)

    # 同键重放：不产生新订单
    n = len(client.get("/api/reservations").json())
    replay = client.post(f"/api/fleet/batches/{batch_id}/atomic-confirm",
                         json={"idempotency_key": "key-atomic-solve-02"})
    assert replay.status_code == 200 and len(client.get("/api/reservations").json()) == n


# ---------- 6. 枪口失能：维护窗口触发批次即时重算 ----------

def test_maintenance_outage_recalculates_batch(client):
    # 逐车路线先达成 READY（A + C）
    _confirm_item(client, F8_BATCH, 1, "key-f8-m-a-000001")
    _cancel_ref(client, "R-SEEDN01", "key-f8-m-rel-0001")
    ready = _confirm_item(client, F8_BATCH, 3, "key-f8-m-c-000001").json()
    assert ready["status"] == "READY"
    a_rid = next(i for i in ready["items"] if i["id"] == 1)["reservation_id"]

    # N1 在预约窗内突发停用
    impact = client.post("/api/operations/maintenance", json={
        "connector_id": GUN_N1, "kind": "OUTAGE",
        "start_at": dt("05:00"), "planned_end_at": dt("05:30"),
        "reason": "N1 突发故障", "idempotency_key": "key-f8-m-out-001",
    }).json()
    item = next(i for i in impact["impacts"] if i["reservation_id"] == a_rid)
    assert item["decision"] == "NEEDS_RESELECTION"

    batch = client.get(f"/api/fleet/batches/{F8_BATCH}").json()
    assert batch["ready_count"] == 1 and batch["status"] == "OPEN"
    assert "ITEM_RESELECTION" in _event_types(batch)
    assert "FLEET_READY_WITHDRAWN" in _event_types(batch)
    a_item = next(i for i in batch["items"] if i["id"] == 1)
    assert a_item["status"] == "RESELECTED" and a_item["access_code"] is None


def test_service_action_rescue_restores_readiness(client):
    _confirm_item(client, F8_BATCH, 1, "key-f8-r-a-000001")
    _cancel_ref(client, "R-SEEDN01", "key-f8-r-rel-0001")
    ready = _confirm_item(client, F8_BATCH, 3, "key-f8-r-c-000001").json()
    a_rid = next(i for i in ready["items"] if i["id"] == 1)["reservation_id"]

    client.post("/api/operations/maintenance", json={
        "connector_id": GUN_N1, "kind": "OUTAGE",
        "start_at": dt("05:00"), "planned_end_at": dt("05:30"),
        "reason": "N1 故障", "idempotency_key": "key-f8-r-out-001",
    })
    # 提前恢复 N2，使它在 05:00-05:30 全程可用，再由客服改派
    n2_window = next(
        w for w in client.get("/api/maintenance").json()
        if w["connector_id"] == GUN_N2 and w["ended_at"] is None
    )
    client.post(f"/api/operations/maintenance/{n2_window['id']}/recover", json={
        "ended_at": dt("04:55"), "idempotency_key": "key-f8-r-rec-001",
    })
    mig = client.post("/api/operations/service-actions", json={
        "reservation_id": a_rid, "kind": "ALTERNATIVE_CONNECTOR",
        "target_connector_id": GUN_N2, "note": "客服改派 N2",
        "idempotency_key": "key-f8-r-svc-001",
    })
    assert mig.status_code == 200

    batch = client.get(f"/api/fleet/batches/{F8_BATCH}").json()
    assert batch["status"] == "READY" and batch["ready_count"] == 2
    assert _event_types(batch)[-1] == "FLEET_READY"


# ---------- 7. 临时换车：旧子预约作废并立即重算 ----------

def test_replan_cancels_old_reservation_and_recalculates(client):
    ready = _confirm_item(client, F8_BATCH, 1, "key-f8-p-a-000001").json()
    _cancel_ref(client, "R-SEEDN01", "key-f8-p-rel-0001")
    ready = _confirm_item(client, F8_BATCH, 3, "key-f8-p-c-000001").json()
    assert ready["status"] == "READY"
    c_item = next(i for i in ready["items"] if i["id"] == 3)
    old_rid = c_item["reservation_id"]

    # C 车临时换成 V7（120kW 车辆档案）
    resp = client.post(f"/api/fleet/batches/{F8_BATCH}/items/3/replan", json={
        "vehicle_id": V7, "idempotency_key": "key-f8-p-rep-001",
    })
    assert resp.status_code == 200
    body = resp.json()
    new_c = next(i for i in body["items"] if i["id"] == 3)
    assert new_c["status"] == "PENDING" and new_c["reservation_id"] is None
    assert client.get(f"/api/reservations/{old_rid}").json()["status"] == "CANCELLED"
    assert body["ready_count"] == 1 and body["status"] == "OPEN"
    assert "ITEM_REPLANNED" in _event_types(body)

    # 换车接口幂等：重放不再取消一次、不产生新预约
    n = len(client.get("/api/reservations").json())
    replay = client.post(f"/api/fleet/batches/{F8_BATCH}/items/3/replan", json={
        "vehicle_id": V7, "idempotency_key": "key-f8-p-rep-001",
    })
    assert replay.status_code == 200 and len(client.get("/api/reservations").json()) == n


# ---------- 8. 建档约束与路线守卫 ----------

def test_duplicate_fleet_no_and_route_guards(client):
    # 同车队编号重复建档（新幂等键）必须被拒
    items = [_fleet_item_body("A 车", V6, 120, "05:30")]
    dup = _create_batch(client, "F-8", "GRADUAL", 1, items, "key-f8-dup-0001")
    assert dup.status_code == 409 and "已存在批次" in dup.json()["detail"]

    # 逐车批次不能走原子接口
    wrong1 = client.post(f"/api/fleet/batches/{F8_BATCH}/atomic-confirm",
                         json={"idempotency_key": "key-f8-guard-001"})
    assert wrong1.status_code == 409 and "逐车确认路线" in wrong1.json()["detail"]

    # 原子批次不能逐车确认
    atomic_id = _atomic_batch(client, fleet_no="F-11", key="key-f8-guard-create")
    wrong2 = _confirm_item(client, atomic_id, 4, "key-f8-guard-002")
    assert wrong2.status_code == 409 and "原子组合路线" in wrong2.json()["detail"]


def test_batch_create_without_vehicle_id_makes_dedicated_vehicle(client):
    resp = _create_batch(
        client, "F-12", "GRADUAL", 1,
        [{
            "vehicle_label": "临时编组车",
            "connector_type": "CCS",
            "min_power_kw": 90,
            "arrival_start": dt("05:00"),
            "arrival_end": dt("05:30"),
            "departure_at": dt("06:00"),
        }],
        "key-f8-novid-0001",
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["vehicle_id"] is not None and item["connector_type"] == "CCS"


def test_min_ready_cannot_exceed_item_count(client):
    items = [_fleet_item_body("A 车", V6, 120, "05:30")]
    resp = _create_batch(client, "F-13", "GRADUAL", 2, items, "key-f8-badmin-001")
    assert resp.status_code == 422 and "最低就绪数" in resp.json()["detail"]


# ---------- 9. 子预约可追溯：普通预约与批次子预约互不混淆 ----------

def test_sub_reservations_are_traceable_to_batch_items(client):
    _confirm_item(client, F8_BATCH, 1, "key-f8-trace-001")
    reservations = client.get("/api/reservations").json()
    sub = next(r for r in reservations if r["fleet_batch_item_id"] == 1)
    assert sub["site_id"] == NORTH and sub["status"] == "CONFIRMED"
    # 个人预约与种子预约不属于任何批次
    others = [r for r in reservations if r["ref"] in ("R-SEEDA01", "R-SEEDN01")]
    assert all(r["fleet_batch_item_id"] is None for r in others)
