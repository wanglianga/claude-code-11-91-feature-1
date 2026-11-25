from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .enums import (
    FleetBatchStatus,
    FleetItemStatus,
    FleetStrategy,
    ImpactDecision,
    MaintenanceKind,
    ReservationMode,
    ReservationStatus,
    ServiceActionKind,
)

# 数据库用 Numeric 保证精度，API 统一以 JSON number 输出
DecimalNum = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v) if v is not None else None, return_type=float | None),
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 基础资源 ----------

class VehicleOut(ORMModel):
    id: int
    name: str
    connector_type: str
    max_power_kw: DecimalNum
    accessible: bool


class ConnectorOut(ORMModel):
    id: int
    code: str
    connector_type: str
    rated_power_kw: DecimalNum
    accessible: bool
    position_note: str


class SiteOut(ORMModel):
    id: int
    name: str
    location: str
    connectors: list[ConnectorOut]


# ---------- 候选搜索 ----------

class SearchRequest(BaseModel):
    vehicle_id: int
    site_id: int
    start_at: datetime
    duration_minutes: int = Field(gt=0, le=600)
    min_power_kw: float = Field(gt=0)
    mode: ReservationMode = ReservationMode.CONNECTOR


class RejectReason(BaseModel):
    connector_code: str
    reasons: list[str]


class SegmentInfo(BaseModel):
    start_at: datetime
    end_at: datetime
    available_power_kw: float
    note: str = ""


class ConnectorCandidate(BaseModel):
    connector_id: int
    connector_code: str
    eligible: bool
    effective_power_kw: float          # 时间窗内最差（最小）有效功率
    locked_by_ref: str | None = None   # 被哪笔有效预约占用
    segments: list[SegmentInfo]
    reasons: list[str]                 # 不合格原因


class PoolCandidate(BaseModel):
    site_id: int
    mode: ReservationMode
    eligible: bool
    # 时间窗内可同时履约的最少合格枪数（容量瓶颈）
    confirmable_slots: int
    window_segments: list[SegmentInfo]
    connector_candidates: list[ConnectorCandidate]


# ---------- 预约 ----------

class ConfirmRequest(BaseModel):
    vehicle_id: int
    site_id: int
    start_at: datetime
    duration_minutes: int = Field(gt=0, le=600)
    min_power_kw: float = Field(gt=0)
    mode: ReservationMode = ReservationMode.CONNECTOR
    # 固定枪口模式可指定偏好枪口；为空时由后端选择一支合格枪并锁住
    preferred_connector_id: int | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


class ReservationEventOut(ORMModel):
    id: int
    event_type: str
    detail: str
    created_at: datetime


class ServiceActionOut(ORMModel):
    id: int
    kind: ServiceActionKind
    order_status_at_action: ReservationStatus
    connector_state_snapshot: str
    target_connector_id: int | None
    amount: DecimalNum | None
    note: str
    created_at: datetime


class ReservationOut(ORMModel):
    id: int
    ref: str
    vehicle_id: int
    site_id: int
    mode: ReservationMode
    status: ReservationStatus
    start_at: datetime
    end_at: datetime
    min_power_kw: DecimalNum
    connector_id: int | None
    confirmation_snapshot: str
    fleet_batch_item_id: int | None = None
    created_at: datetime
    events: list[ReservationEventOut] = []
    service_actions: list[ServiceActionOut] = []


class TransitionRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)


# ---------- 维护 ----------

class MaintenanceCreate(BaseModel):
    connector_id: int
    kind: MaintenanceKind
    start_at: datetime
    planned_end_at: datetime
    effective_power_kw: float | None = None
    reason: str = ""
    idempotency_key: str = Field(min_length=8, max_length=64)


class MaintenanceRecover(BaseModel):
    ended_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=64)


class MaintenanceOut(ORMModel):
    id: int
    connector_id: int
    kind: MaintenanceKind
    start_at: datetime
    planned_end_at: datetime
    ended_at: datetime | None
    effective_power_kw: DecimalNum | None
    reason: str


class ImpactItem(BaseModel):
    reservation_id: int
    ref: str
    status: ReservationStatus
    mode: ReservationMode
    connector_id: int | None
    decision: ImpactDecision
    reason: str
    overlap_start: datetime | None
    overlap_end: datetime | None


class MaintenanceImpactOut(BaseModel):
    maintenance: MaintenanceOut
    impacts: list[ImpactItem]


# ---------- 客服动作 ----------

class ServiceActionCreate(BaseModel):
    reservation_id: int
    kind: ServiceActionKind
    target_connector_id: int | None = None
    amount: float | None = None
    note: str = ""
    idempotency_key: str = Field(min_length=8, max_length=64)


# ---------- 车队批次 ----------

class FleetItemRequest(BaseModel):
    """逐车需求：接口、最低目标电量、到站范围与出发时刻。"""

    vehicle_label: str = Field(min_length=1, max_length=64)
    vehicle_id: int | None = None
    connector_type: str = Field(min_length=1, max_length=16)
    min_power_kw: float = Field(gt=0)
    accessible_required: bool = False
    arrival_start: datetime
    arrival_end: datetime
    departure_at: datetime
    mode: ReservationMode = ReservationMode.CONNECTOR


class FleetBatchCreate(BaseModel):
    fleet_no: str = Field(min_length=2, max_length=32)
    site_id: int
    strategy: FleetStrategy
    min_ready: int = Field(ge=1)
    items: list[FleetItemRequest] = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=64)


class FleetItemOut(ORMModel):
    id: int
    position: int
    vehicle_label: str
    vehicle_id: int | None
    connector_type: str
    min_power_kw: DecimalNum
    accessible_required: bool
    mode: ReservationMode
    arrival_start: datetime
    arrival_end: datetime
    departure_at: datetime
    status: FleetItemStatus
    reservation_id: int | None
    reservation_ref: str | None = None
    reservation_status: str | None = None
    connector_id: int | None = None
    last_reason: str
    access_code: str | None
    navigation: str | None
    notified_at: datetime | None


class FleetBatchEventOut(ORMModel):
    id: int
    event_type: str
    detail: str
    created_at: datetime


class FleetBatchOut(ORMModel):
    id: int
    fleet_no: str
    site_id: int
    strategy: FleetStrategy
    min_ready: int
    status: FleetBatchStatus
    ready_count: int
    ready_declared: bool
    ready_at: datetime | None
    created_at: datetime
    items: list[FleetItemOut]
    events: list[FleetBatchEventOut] = []


class FleetUnmetItemOut(BaseModel):
    item_id: int
    position: int
    vehicle_label: str
    status: str
    reason: str


class FleetBatchDetailOut(FleetBatchOut):
    # 车队视图：距离最低就绪数还差几辆 + 未满足车辆及原因
    deficit: int
    unmet_items: list[FleetUnmetItemOut]


class FleetConfirmOut(FleetBatchDetailOut):
    # 逐车确认结果：CONFIRMED 成功持有容量；REJECTED 容量不足，原因已落到车辆行
    outcome: str
    outcome_item_id: int
    message: str


class FleetItemConfirmRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    # 可指定偏好枪口（固定枪口模式）；为空由后端挑选合格枪
    preferred_connector_id: int | None = None


class FleetReplanRequest(BaseModel):
    """车辆临时换车：改写该车的接口/功率/无障碍/出发时刻等需求后立即重算。"""

    vehicle_label: str | None = Field(default=None, max_length=64)
    vehicle_id: int | None = None
    connector_type: str | None = Field(default=None, max_length=16)
    min_power_kw: float | None = Field(default=None, gt=0)
    accessible_required: bool | None = None
    arrival_start: datetime | None = None
    arrival_end: datetime | None = None
    departure_at: datetime | None = None
    mode: ReservationMode | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)
