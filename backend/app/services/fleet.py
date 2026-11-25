"""车队批次用例：带车队编号的批次请求、两条确认路线与即时重算。

两条路线（FleetStrategy）：
- GRADUAL 逐车确认：每辆车单独走确认事务，成功者立即签发导航/进场码并累计
  批次进度；单车确认只是单车承诺，批次是否“就绪”只看持有容量的子预约数是否
  达到最低就绪数 min_ready。
- ATOMIC 原子组合：在同一事务（保存点）内回溯尝试整组兼容预约，达到 min_ready
  才整体落库；任一环节失败导致数量不足时整组回滚，不留下任何容量占用，
  绝不会把部分成功伪装成批次承诺。

重算触发：子预约取消/迟到、维护窗口导致改选、客服救回、临时换车之后，
统一由 reconcile_batch 对账：就绪数跨越最低线时发出“车队就绪”或“就绪撤回”
通知，已发出的单车确认不会被等同为车队就绪。

并发与幂等：所有写操作沿用站点级 pg_advisory_xact_lock 与路由层幂等框架，
重复提交同一批次/同一动作只会回放首次结果，不会多占容量。
"""

import json
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..enums import (
    HOLDING_STATUSES,
    FleetBatchStatus,
    FleetItemStatus,
    FleetStrategy,
    ReservationStatus,
)
from ..models import (
    FleetBatch,
    FleetBatchEvent,
    FleetBatchItem,
    Reservation,
    Site,
    Vehicle,
)
from ..schemas import FleetBatchCreate, FleetReplanRequest
from .capacity import evaluate_site
from .reservation import _add_event, create_reservation_locked

LOCK_NS_SITE = 91001
LOCK_NS_FLEET = 91004


def _new_access_code() -> str:
    return f"G-{uuid4().hex[:6].upper()}"


def _add_batch_event(db: Session, batch: FleetBatch, event_type: str, detail: dict) -> None:
    event = FleetBatchEvent(
        batch_id=batch.id,
        event_type=event_type,
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )
    db.add(event)
    batch.events.append(event)
    db.flush()


def _lock(db: Session, ns: int, key: int) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": ns, "k": key})


# ---------- 批次创建 ----------

def create_batch(db: Session, req: FleetBatchCreate) -> FleetBatch:
    site = db.get(Site, req.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if db.scalars(
        select(FleetBatch).where(FleetBatch.fleet_no == req.fleet_no)
    ).first() is not None:
        raise HTTPException(409, f"车队编号 {req.fleet_no} 已存在批次，不能重复建档")
    if req.min_ready > len(req.items):
        raise HTTPException(422, "最低就绪数不能大于批次内车辆数")

    batch = FleetBatch(
        fleet_no=req.fleet_no,
        site_id=site.id,
        strategy=req.strategy,
        min_ready=req.min_ready,
        status=FleetBatchStatus.OPEN,
        ready_count=0,
        ready_declared=False,
    )
    db.add(batch)
    db.flush()

    for pos, item_req in enumerate(req.items, start=1):
        if not (item_req.arrival_start < item_req.arrival_end <= item_req.departure_at):
            raise HTTPException(
                422,
                f"第 {pos} 辆车的到站范围/出发时刻不合法：需满足 到站起 < 到站止 <= 出发时刻",
            )
        vehicle = (
            db.get(Vehicle, item_req.vehicle_id)
            if item_req.vehicle_id is not None else None
        )
        if item_req.vehicle_id is not None and vehicle is None:
            raise HTTPException(404, f"第 {pos} 辆车指定的 vehicle_id 不存在")
        if vehicle is None:
            # 无既有车辆档案时，为该车建立一辆与需求匹配的专用车辆（FK 需要真实车辆行）
            vehicle = Vehicle(
                name=f"{req.fleet_no} · {item_req.vehicle_label}",
                connector_type=item_req.connector_type,
                max_power_kw=item_req.min_power_kw,
                accessible=item_req.accessible_required,
            )
            db.add(vehicle)
            db.flush()
        item = FleetBatchItem(
            batch_id=batch.id,
            position=pos,
            vehicle_label=item_req.vehicle_label,
            vehicle_id=vehicle.id,
            connector_type=item_req.connector_type,
            min_power_kw=item_req.min_power_kw,
            accessible_required=item_req.accessible_required,
            mode=item_req.mode,
            arrival_start=item_req.arrival_start,
            arrival_end=item_req.arrival_end,
            departure_at=item_req.departure_at,
            status=FleetItemStatus.PENDING,
        )
        db.add(item)
    db.flush()
    _add_batch_event(db, batch, "BATCH_CREATED", {
        "fleet_no": batch.fleet_no,
        "strategy": batch.strategy.value,
        "min_ready": batch.min_ready,
        "vehicles": [i.vehicle_label for i in batch.items],
        "note": "批次已建档，尚未持有任何容量",
    })
    return batch


# ---------- 视图组装（含未满足车辆及原因） ----------

def _item_unmet_reason(item: FleetBatchItem) -> str:
    if item.reservation is not None and item.reservation.status in HOLDING_STATUSES:
        return ""
    if item.last_reason:
        return item.last_reason
    if item.reservation is not None and item.reservation.status == ReservationStatus.COMPLETED:
        return "子预约已完成，不再计入本次出车的就绪数"
    return {
        FleetItemStatus.PENDING: "尚未确认子预约",
        FleetItemStatus.FAILED: "最近一次确认失败",
        FleetItemStatus.CANCELLED: "子预约已取消，容量已释放",
        FleetItemStatus.RESELECTED: "枪口失能/维护影响，子预约进入改选，不再计入就绪数",
        FleetItemStatus.CONFIRMED: "子预约已不在持有容量状态",
    }.get(item.status, "未满足最低就绪数")


def batch_view(db: Session, batch: FleetBatch) -> dict:
    db.refresh(batch)
    unmet = []
    for item in batch.items:
        reason = _item_unmet_reason(item)
        if reason:
            unmet.append({
                "item_id": item.id,
                "position": item.position,
                "vehicle_label": item.vehicle_label,
                "status": item.status.value,
                "reason": reason,
            })
    return {
        "id": batch.id,
        "fleet_no": batch.fleet_no,
        "site_id": batch.site_id,
        "strategy": batch.strategy,
        "min_ready": batch.min_ready,
        "status": batch.status,
        "ready_count": batch.ready_count,
        "ready_declared": batch.ready_declared,
        "ready_at": batch.ready_at,
        "created_at": batch.created_at,
        "deficit": max(0, batch.min_ready - batch.ready_count),
        "unmet_items": unmet,
        "items": batch.items,
        "events": batch.events,
    }


# ---------- 批次重算：取消 / 失能 / 救回 / 换车后立即对账 ----------

def reconcile_batch(db: Session, batch_id: int, trigger: str = "RECONCILE") -> FleetBatch:
    """按子预约的真实持有状态重算每一行与批次就绪数，并在跨线时发通知/撤回。"""
    _lock(db, LOCK_NS_FLEET, batch_id)
    batch = db.get(FleetBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "车队批次不存在")

    ready = 0
    for item in batch.items:
        reservation = (
            db.get(Reservation, item.reservation_id)
            if item.reservation_id is not None else None
        )
        if reservation is not None and reservation.status in HOLDING_STATUSES:
            ready += 1
            if item.status != FleetItemStatus.CONFIRMED:
                item.status = FleetItemStatus.CONFIRMED
                item.last_reason = ""
            continue

        if reservation is None:
            continue

        new_status = None
        event_type = None
        if reservation.status in (ReservationStatus.CANCELLED, ReservationStatus.NO_SHOW):
            new_status = FleetItemStatus.CANCELLED
            event_type = "ITEM_CANCELLED"
        elif reservation.status in (
            ReservationStatus.NEEDS_RESELECTION,
            ReservationStatus.MANUAL_CONTACT,
        ):
            new_status = FleetItemStatus.RESELECTED
            event_type = "ITEM_RESELECTION"
        if new_status is not None and item.status != new_status:
            item.status = new_status
            item.access_code = None  # 进场码随容量释放/失能一并作废
            item.last_reason = _item_unmet_reason(item)
            _add_batch_event(db, batch, event_type, {
                "trigger": trigger,
                "item_id": item.id,
                "vehicle_label": item.vehicle_label,
                "reservation_ref": reservation.ref,
                "reservation_status": reservation.status.value,
            })

    batch.ready_count = ready
    if ready >= batch.min_ready:
        batch.status = FleetBatchStatus.READY
        if not batch.ready_declared:
            batch.ready_declared = True
            batch.ready_at = datetime.now()
            _add_batch_event(db, batch, "FLEET_READY", {
                "trigger": trigger,
                "ready_count": ready,
                "min_ready": batch.min_ready,
                "note": f"已有 {ready} 辆车持有有效子预约，达到最低就绪数 {batch.min_ready}",
            })
    else:
        if batch.ready_declared:
            _add_batch_event(db, batch, "FLEET_READY_WITHDRAWN", {
                "trigger": trigger,
                "ready_count": ready,
                "min_ready": batch.min_ready,
                "deficit": batch.min_ready - ready,
                "note": "就绪数跌破最低线，已宣布的车队就绪撤回，禁止再发车队就绪通知",
            })
        batch.ready_declared = False
        batch.ready_at = None
        batch.status = FleetBatchStatus.OPEN
    db.flush()
    return batch


def reconcile_for_reservation(db: Session, reservation: Reservation, trigger: str) -> None:
    """状态机/维护/客服动作改变子预约后调用的轻量钩子。"""
    if reservation.fleet_batch_item_id is None:
        return
    item = db.get(FleetBatchItem, reservation.fleet_batch_item_id)
    if item is not None:
        reconcile_batch(db, item.batch_id, trigger=trigger)


# ---------- 逐车确认（GRADUAL） ----------

def _navigation(site: Site, item: FleetBatchItem, reservation: Reservation) -> str:
    where = "到站后由资源池分配枪口" if reservation.connector_id is None else "按锁定枪口指引停靠"
    return f"导航至 {site.name}（{site.location}）；{where}；请在 {item.arrival_start:%H:%M}-{item.arrival_end:%H:%M} 到站"


def _confirm_item_locked(
    db: Session, batch: FleetBatch, item: FleetBatchItem,
    preferred_connector_id: int | None,
) -> Reservation:
    site = db.get(Site, batch.site_id)
    vehicle = db.get(Vehicle, item.vehicle_id)
    # 子预约只占用“到站范围”[arrival_start, arrival_end)；
    # departure_at 是车队出发/就绪截止时刻，不延长枪口占用
    return create_reservation_locked(
        db,
        site=site,
        vehicle=vehicle,
        start=item.arrival_start,
        end=item.arrival_end,
        min_power_kw=float(item.min_power_kw),
        mode=item.mode,
        preferred_connector_id=preferred_connector_id,
        fleet_batch_item_id=item.id,
    )


def confirm_item(
    db: Session, batch_id: int, item_id: int, preferred_connector_id: int | None
) -> tuple[FleetBatch, str, str]:
    """逐车确认。返回 (批次, outcome, message)；容量不足时 outcome=REJECTED 且不抛错，
    拒绝原因落到车辆行，批次状态照常重算（界面必须保持“未达最低就绪数”）。"""
    _lock(db, LOCK_NS_FLEET, batch_id)
    batch = db.get(FleetBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "车队批次不存在")
    if batch.strategy != FleetStrategy.GRADUAL:
        raise HTTPException(
            409,
            "该批次采用原子组合路线，请调用原子确认接口一次性求解，"
            "不允许逐车卖出部分容量",
        )
    item = next((i for i in batch.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(404, "批次内不存在该车辆行")
    existing = (
        db.get(Reservation, item.reservation_id)
        if item.reservation_id is not None else None
    )
    if existing is not None and existing.status in HOLDING_STATUSES:
        raise HTTPException(409, f"{item.vehicle_label} 已有在途子预约 {existing.ref}，不能重复确认")

    _lock(db, LOCK_NS_SITE, batch.site_id)
    try:
        reservation = _confirm_item_locked(db, batch, item, preferred_connector_id)
    except HTTPException as exc:
        # 单车确认被拒不影响其他车辆：原因落库、批次重算，整体请求仍返回可展示结果
        item.status = FleetItemStatus.FAILED
        item.last_reason = _reason_text(exc.detail)
        _add_batch_event(db, batch, "ITEM_CONFIRM_FAILED", {
            "item_id": item.id,
            "vehicle_label": item.vehicle_label,
            "reason": exc.detail,
        })
        reconcile_batch(db, batch.id, trigger="ITEM_CONFIRM_FAILED")
        return batch, "REJECTED", f"{item.vehicle_label} 确认被拒：{_flatten_reason(exc.detail)}"

    item.reservation_id = reservation.id
    item.status = FleetItemStatus.CONFIRMED
    item.last_reason = ""
    item.access_code = _new_access_code()
    item.navigation = _navigation(db.get(Site, batch.site_id), item, reservation)
    item.notified_at = datetime.now()

    _add_batch_event(db, batch, "ITEM_CONFIRMED", {
        "item_id": item.id,
        "vehicle_label": item.vehicle_label,
        "reservation_ref": reservation.ref,
        "connector_id": reservation.connector_id,
    })
    _add_batch_event(db, batch, "DRIVER_NOTIFY", {
        "item_id": item.id,
        "vehicle_label": item.vehicle_label,
        "reservation_ref": reservation.ref,
        "access_code": item.access_code,
        "navigation": item.navigation,
        "note": "车辆准备较早即可先收到导航与进场码；该通知不代表整个车队已就绪",
    })
    reconcile_batch(db, batch.id, trigger="ITEM_CONFIRMED")
    return batch, "CONFIRMED", f"{item.vehicle_label} 子预约 {reservation.ref} 已确认，进场码 {item.access_code}"


def _flatten_reason(detail) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return detail.get("message") or json.dumps(detail, ensure_ascii=False)
    return str(detail)


# ---------- 原子组合确认（ATOMIC） ----------

def _reason_text(detail) -> str:
    return json.dumps(detail, ensure_ascii=False, default=str)


class _Unsolved(Exception):
    """内部信号：原子组合达不到最低就绪数，保存点已回滚。"""


def solve_atomic(
    db: Session, batch_id: int
) -> tuple[FleetBatch, str, str]:
    """原子组合求解。

    返回 (批次, outcome, message)：outcome=CONFIRMED 表示整组达成最低就绪数；
    outcome=REJECTED 表示兼容组合不足，试算保存点已整体回滚、零容量占用，
    失败原因与 ATOMIC_SOLVE_FAILED 事件保留在外层事务供车队视图展示。
    """
    _lock(db, LOCK_NS_FLEET, batch_id)
    batch = db.get(FleetBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "车队批次不存在")
    if batch.strategy != FleetStrategy.ATOMIC:
        raise HTTPException(409, "该批次采用逐车确认路线，请逐车调用确认接口")

    def _holding(item: FleetBatchItem) -> bool:
        if item.reservation_id is None:
            return False
        r = db.get(Reservation, item.reservation_id)
        return r is not None and r.status in HOLDING_STATUSES

    pending = [i for i in batch.items if not _holding(i)]
    already = len(batch.items) - len(pending)
    if not pending:
        batch = reconcile_batch(db, batch.id, trigger="ATOMIC_NOOP")
        return batch, "CONFIRMED", "批次车辆均已持有有效子预约，无需再次求解"

    _lock(db, LOCK_NS_SITE, batch.site_id)
    site = db.get(Site, batch.site_id)

    # 先用只读容量评估按“候选枪数升序”排序（最受限的车辆先尝试），提升一次求解成功率
    def restrictiveness(item: FleetBatchItem) -> tuple:
        vehicle = db.get(Vehicle, item.vehicle_id)
        evaluation = evaluate_site(
            db, site.id, vehicle,
            item.arrival_start, item.arrival_end, float(item.min_power_kw),
        )
        eligible = sum(1 for ev in evaluation.connector_evals if ev.eligible)
        return (eligible, -float(item.min_power_kw), item.position)

    pending.sort(key=restrictiveness)

    # 整组尝试放在独立保存点内：数量不足时整体回滚，零容量占用
    trial = db.begin_nested()
    confirmed: list[tuple[FleetBatchItem, Reservation]] = []
    failures: dict[int, object] = {}
    try:
        for item in pending:
            try:
                with db.begin_nested():
                    reservation = _confirm_item_locked(db, batch, item, None)
                confirmed.append((item, reservation))
            except HTTPException as exc:
                failures[item.id] = exc.detail
        if already + len(confirmed) < batch.min_ready:
            trial.rollback()
            raise _Unsolved()
    except _Unsolved:
        # 回滚后 ORM 对象已过期，重新装载批次与车辆行，再把失败原因与事件落到外层事务
        batch = db.get(FleetBatch, batch_id)
        unmet = []
        for item in batch.items:
            if item.id in failures:
                item.status = FleetItemStatus.FAILED
                item.last_reason = _reason_text(failures[item.id])
                unmet.append({
                    "item_id": item.id,
                    "vehicle_label": item.vehicle_label,
                    "reason": failures[item.id],
                })
        _add_batch_event(db, batch, "ATOMIC_SOLVE_FAILED", {
            "confirmed_in_trial": len(confirmed),
            "min_ready": batch.min_ready,
            "unmet": unmet,
            "note": "兼容组合达不到最低就绪数，整组回滚，未占用任何容量",
        })
        reconcile_batch(db, batch.id, trigger="ATOMIC_SOLVE_FAILED")
        message = (
            f"求不出达到最低就绪数 {batch.min_ready} 的兼容预约组合，"
            f"试算最多只能满足 {already + len(confirmed)} 辆；本次原子确认整体拒绝"
            "（无部分成功、零容量占用）"
        )
        return batch, "REJECTED", message

    # 成功：保存点内的全部子预约保留，由外层事务统一提交
    for item, reservation in confirmed:
        item.reservation_id = reservation.id
        item.status = FleetItemStatus.CONFIRMED
        item.last_reason = ""
        item.access_code = _new_access_code()
        item.navigation = _navigation(site, item, reservation)
        item.notified_at = datetime.now()
        _add_batch_event(db, batch, "ITEM_CONFIRMED", {
            "item_id": item.id,
            "vehicle_label": item.vehicle_label,
            "reservation_ref": reservation.ref,
            "connector_id": reservation.connector_id,
            "atomic": True,
        })
        _add_batch_event(db, batch, "DRIVER_NOTIFY", {
            "item_id": item.id,
            "vehicle_label": item.vehicle_label,
            "reservation_ref": reservation.ref,
            "access_code": item.access_code,
            "navigation": item.navigation,
            "note": "原子组合达成后统一签发；所有司机在完整组合算出后才收到结果",
        })
    for item in pending:
        if item.id in failures:
            item.status = FleetItemStatus.FAILED
            item.last_reason = _reason_text(failures[item.id])
            _add_batch_event(db, batch, "ITEM_CONFIRM_FAILED", {
                "item_id": item.id,
                "vehicle_label": item.vehicle_label,
                "reason": failures[item.id],
                "note": "原子组合已满足最低就绪数，该车为备用车，未占用容量",
            })
    _add_batch_event(db, batch, "ATOMIC_SOLVE_SUCCEEDED", {
        "confirmed": len(confirmed),
        "min_ready": batch.min_ready,
    })
    batch = reconcile_batch(db, batch.id, trigger="ATOMIC_SOLVE_SUCCEEDED")
    return batch, "CONFIRMED", (
        f"原子组合求解成功：{len(confirmed)} 笔子预约一次性确认，"
        f"车队就绪数 {batch.ready_count}/{batch.min_ready}，通知已统一签发"
    )


# ---------- 临时换车 ----------

def replan_item(
    db: Session, batch_id: int, item_id: int, req: FleetReplanRequest
) -> FleetBatch:
    _lock(db, LOCK_NS_FLEET, batch_id)
    batch = db.get(FleetBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "车队批次不存在")
    item = next((i for i in batch.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(404, "批次内不存在该车辆行")

    _lock(db, LOCK_NS_SITE, batch.site_id)
    reservation = (
        db.get(Reservation, item.reservation_id)
        if item.reservation_id is not None else None
    )
    detached_ref = None
    if reservation is not None and reservation.status in HOLDING_STATUSES:
        # 换车即作废旧承诺：取消在途子预约，容量立即释放
        reservation.status = ReservationStatus.CANCELLED
        _add_event(db, reservation, "CANCELLED", {
            "released_connector_id": reservation.connector_id,
            "note": f"车队 {batch.fleet_no} 临时换车，原子预约作废",
        })
        item.access_code = None
        item.notified_at = None
        detached_ref = reservation.ref
    elif reservation is not None:
        # 旧子预约已失效（取消/改选等）：换车时直接脱离，历史预约仍可经事件与外键追溯
        detached_ref = reservation.ref

    if req.vehicle_id is not None:
        new_vehicle = db.get(Vehicle, req.vehicle_id)
        if new_vehicle is None:
            raise HTTPException(404, "换车目标车辆不存在")
        item.vehicle_id = new_vehicle.id
    else:
        vehicle = db.get(Vehicle, item.vehicle_id)
        is_dedicated = vehicle is not None and vehicle.name.startswith(f"{batch.fleet_no} ·")
        if not is_dedicated:
            raise HTTPException(
                422,
                "该车辆为共享车辆档案，不能直接改写需求；请通过 vehicle_id 指定替换车辆",
            )
        if req.connector_type is not None:
            vehicle.connector_type = req.connector_type
            item.connector_type = req.connector_type
        if req.min_power_kw is not None:
            vehicle.max_power_kw = req.min_power_kw
            item.min_power_kw = req.min_power_kw
        if req.accessible_required is not None:
            vehicle.accessible = req.accessible_required
            item.accessible_required = req.accessible_required

    if req.vehicle_label is not None:
        item.vehicle_label = req.vehicle_label
    if req.arrival_start is not None:
        item.arrival_start = req.arrival_start
    if req.arrival_end is not None:
        item.arrival_end = req.arrival_end
    if req.departure_at is not None:
        item.departure_at = req.departure_at
    if req.mode is not None:
        item.mode = req.mode
    if not (item.arrival_start < item.arrival_end <= item.departure_at):
        raise HTTPException(422, "换车后的到站范围/出发时刻不合法")

    item.status = FleetItemStatus.PENDING
    if detached_ref is not None:
        item.reservation_id = None
    item.last_reason = "车辆临时换车，旧子预约已作废，需按新需求重新确认"
    _add_batch_event(db, batch, "ITEM_REPLANNED", {
        "item_id": item.id,
        "vehicle_label": item.vehicle_label,
        "connector_type": item.connector_type,
        "min_power_kw": float(item.min_power_kw),
        "detached_reservation_ref": detached_ref,
    })
    reconcile_batch(db, batch.id, trigger="ITEM_REPLANNED")
    return batch
