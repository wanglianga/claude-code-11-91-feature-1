"""本地样例数据：东站 A/B/C/D 四枪 + 北站 F-8 车队场景 + 样例车队批次。

运行：python -m app.seed
幂等策略：直接 drop/create 全部表后重灌，仅用于本地演示与重置。
"""

import json
from datetime import datetime

from .database import engine, SessionLocal
from .enums import (
    FleetItemStatus,
    FleetStrategy,
    MaintenanceKind,
    ReservationMode,
    ReservationStatus,
)
from .models import (
    Base,
    Connector,
    FleetBatch,
    FleetBatchItem,
    MaintenanceWindow,
    Reservation,
    ReservationEvent,
    Site,
    Vehicle,
)

# 固定在“周五”这一天（2026-11-27 为周五），演示统一使用朴素本地时间
FRIDAY = "2026-11-27"


def _dt(hm: str) -> datetime:
    return datetime.fromisoformat(f"{FRIDAY}T{hm}:00")


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # ---------- 车辆 ----------
        v1 = Vehicle(
            name="沪A·11111 家用轿车（已持有 A 枪预约）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v2 = Vehicle(
            name="沪B·22222 小型车（最大受电仅 60kW）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v3 = Vehicle(
            name="沪C·33333 无障碍车辆（需无障碍车位）",
            connector_type="CCS", max_power_kw=100, accessible=True,
        )
        v4 = Vehicle(
            name="沪D·44444 长续航 SUV（请求 90kW 的第二位车主）",
            connector_type="CCS", max_power_kw=150, accessible=False,
        )
        # 北站 F-8 场景车辆
        v5 = Vehicle(
            name="沪E·55555 个人用户（占用北站 60kW 枪位 05:00-06:00）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v6 = Vehicle(
            name="F-8 · A 车（需 120kW 枪口）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v7 = Vehicle(
            name="F-8 · B 车（需 120kW 枪口）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v8 = Vehicle(
            name="F-8 · C 车（备用车，可接受 60kW）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        db.add_all([v1, v2, v3, v4, v5, v6, v7, v8])
        db.flush()

        # ---------- 东站与枪口 ----------
        site = Site(name="东站充电站", location="东站地下停车场 B1 层西侧")
        db.add(site)
        db.flush()

        connectors = [
            Connector(site_id=site.id, code="A", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 1 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="B", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 2 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="C", connector_type="CCS",
                      rated_power_kw=60, accessible=False,
                      position_note="东侧靠墙，普通车位，额定仅 60kW"),
            Connector(site_id=site.id, code="D", connector_type="CHAdeMO",
                      rated_power_kw=120, accessible=False,
                      position_note="东侧柱后，CHAdeMO 接口，与 CCS 车辆不兼容"),
        ]
        db.add_all(connectors)
        db.flush()
        gun_a, gun_b = connectors[0], connectors[1]

        # ---------- A 枪 18:00-18:30 已存在的有效预约 ----------
        snapshot = {
            "mode": ReservationMode.CONNECTOR.value,
            "connector_id": gun_a.id,
            "connector_code": "A",
            "connector_type": "CCS",
            "min_power_kw": 90,
            "effective_power_kw": 120.0,
            "accessible": True,
            "promise": "锁定枪口 A，全程有效功率不低于 120kW",
        }
        existing = Reservation(
            ref="R-SEEDA01",
            vehicle_id=v1.id,
            site_id=site.id,
            mode=ReservationMode.CONNECTOR,
            status=ReservationStatus.CONFIRMED,
            start_at=_dt("18:00"),
            end_at=_dt("18:30"),
            min_power_kw=90,
            connector_id=gun_a.id,
            confirmation_snapshot=json.dumps(snapshot, ensure_ascii=False),
        )
        db.add(existing)
        db.flush()
        db.add(ReservationEvent(
            reservation_id=existing.id,
            event_type="CONFIRMED",
            detail=json.dumps({"snapshot": snapshot}, ensure_ascii=False),
        ))

        # ---------- 北站：F-8 车队凌晨场景 ----------
        # 05:00-05:30 只有一个 120kW 枪位（N1 在 05:30 后停用）；
        # 05:00-06:00 有一个 60kW 枪位 N3（06:00 后停用），且被个人用户预约占用。
        north = Site(name="北站充电站", location="北站枢纽 P3 停车场车队专区")
        db.add(north)
        db.flush()

        n1 = Connector(site_id=north.id, code="N1", connector_type="CCS",
                       rated_power_kw=120, accessible=False,
                       position_note="车队专区 1 号位，120kW，05:30 起计划停用")
        n2 = Connector(site_id=north.id, code="N2", connector_type="CCS",
                       rated_power_kw=120, accessible=False,
                       position_note="车队专区 2 号位，120kW，05:30 前停用检修")
        n3 = Connector(site_id=north.id, code="N3", connector_type="CCS",
                       rated_power_kw=60, accessible=False,
                       position_note="车队专区 3 号位，60kW，06:00 起计划停用")
        db.add_all([n1, n2, n3])
        db.flush()

        db.add_all([
            MaintenanceWindow(
                connector_id=n1.id, kind=MaintenanceKind.OUTAGE,
                start_at=_dt("05:30"), planned_end_at=_dt("07:00"),
                reason="凌晨例行检修，05:30 后 N1 停用",
            ),
            MaintenanceWindow(
                connector_id=n2.id, kind=MaintenanceKind.OUTAGE,
                start_at=_dt("04:30"), planned_end_at=_dt("05:30"),
                reason="05:30 前 N2 停用检修，凌晨窗内仅 N1 一支 120kW 枪",
            ),
            MaintenanceWindow(
                connector_id=n3.id, kind=MaintenanceKind.OUTAGE,
                start_at=_dt("06:00"), planned_end_at=_dt("07:00"),
                reason="06:00 后 N3 停用检修",
            ),
        ])

        # 个人用户同时占用 60kW 枪位 N3（05:00-06:00）
        personal_snapshot = {
            "mode": ReservationMode.CONNECTOR.value,
            "connector_id": n3.id,
            "connector_code": "N3",
            "connector_type": "CCS",
            "min_power_kw": 60,
            "effective_power_kw": 60.0,
            "accessible": False,
            "promise": "锁定枪口 N3，全程有效功率不低于 60kW",
        }
        personal = Reservation(
            ref="R-SEEDN01",
            vehicle_id=v5.id,
            site_id=north.id,
            mode=ReservationMode.CONNECTOR,
            status=ReservationStatus.CONFIRMED,
            start_at=_dt("05:00"),
            end_at=_dt("06:00"),
            min_power_kw=60,
            connector_id=n3.id,
            confirmation_snapshot=json.dumps(personal_snapshot, ensure_ascii=False),
        )
        db.add(personal)
        db.flush()
        db.add(ReservationEvent(
            reservation_id=personal.id,
            event_type="CONFIRMED",
            detail=json.dumps({"snapshot": personal_snapshot}, ensure_ascii=False),
        ))

        # ---------- 预置车队批次 F-8：最低就绪数 2，逐车确认路线 ----------
        batch = FleetBatch(
            fleet_no="F-8",
            site_id=north.id,
            strategy=FleetStrategy.GRADUAL,
            min_ready=2,
            status="OPEN",
            ready_count=0,
            ready_declared=False,
        )
        db.add(batch)
        db.flush()
        item_specs = [
            ("A 车", v6.id, "CCS", 120, _dt("05:00"), _dt("05:30"), _dt("06:00")),
            ("B 车", v7.id, "CCS", 120, _dt("05:00"), _dt("05:30"), _dt("06:00")),
            ("C 车（备用）", v8.id, "CCS", 60, _dt("05:00"), _dt("06:00"), _dt("06:00")),
        ]
        for pos, (label, vid, ctype, power, arr_s, arr_e, depart) in enumerate(item_specs, start=1):
            db.add(FleetBatchItem(
                batch_id=batch.id,
                position=pos,
                vehicle_label=label,
                vehicle_id=vid,
                connector_type=ctype,
                min_power_kw=power,
                accessible_required=False,
                mode=ReservationMode.CONNECTOR,
                arrival_start=arr_s,
                arrival_end=arr_e,
                departure_at=depart,
                status=FleetItemStatus.PENDING,
            ))
        db.flush()

        db.commit()

        print("种子数据完成：")
        print("  东站: 枪口 A/B=120kW CCS，C=60kW CCS，D=120kW CHAdeMO；R-SEEDA01 锁定 A 枪 18:00-18:30")
        print("  北站: N1=120kW（05:30 起停用），N2=120kW（05:30 前停用），N3=60kW（06:00 起停用）")
        print("  个人预约 R-SEEDN01 占用 N3 枪 05:00-06:00")
        print("  车队批次 F-8（逐车确认，最低就绪数 2）：A/B 需 120kW（到站 05:00-05:30），C 可接受 60kW（到站 05:00-06:00），均未确认")
        print("  => N3 被占用时 A/B 抢唯一 120kW 枪 N1，批次最多就绪 1 辆，界面保持“未达 2 辆”；释放 N3 后 C 可确认即达标")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
