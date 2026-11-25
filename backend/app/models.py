from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    FleetBatchStatus,
    FleetItemStatus,
    FleetStrategy,
    MaintenanceKind,
    ReservationMode,
    ReservationStatus,
    ServiceActionKind,
)


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    connector_type: Mapped[str] = mapped_column(String(16))
    max_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="vehicle")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128))

    connectors: Mapped[list["Connector"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    code: Mapped[str] = mapped_column(String(16))
    connector_type: Mapped[str] = mapped_column(String(16))
    # 枪口额定/当前可用功率上限（kW）。维护降额通过维护窗口的 effective_power_kw 叠加
    rated_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    position_note: Mapped[str] = mapped_column(String(255), default="")

    site: Mapped[Site] = relationship(back_populates="connectors")
    maintenance_windows: Mapped[list["MaintenanceWindow"]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_connector_site_code"),)


class MaintenanceWindow(Base):
    """运营端记录的降额/停用/恢复生效时段。

    恢复 = 给此前的降额/停用窗口设置实际结束时刻（ended_at），
    窗口本身保留，作为历史审计与“按真实交集判断影响”的依据。
    """

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"))
    kind: Mapped[MaintenanceKind] = mapped_column(Enum(MaintenanceKind))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    planned_end_at: Mapped[datetime] = mapped_column(DateTime)
    # 提前恢复时写入实际结束时刻；为空表示仍在生效
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 降额后的功率；停用窗口为空
    effective_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")

    connector: Mapped[Connector] = relationship(back_populates="maintenance_windows")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    mode: Mapped[ReservationMode] = mapped_column(Enum(ReservationMode))
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.CONFIRMED
    )
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    min_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    # 固定枪口模式：确认时锁定；资源池模式：确认时为空，到站分配
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    # 确认时的承诺快照（插头/功率/无障碍/锁定枪口或池名额），后续任何变化不得改写
    confirmation_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    # 车队批次追溯：普通预约为空；批次子预约指向所属批次行（历史多笔可指向同一行）
    fleet_batch_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("fleet_batch_items.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    vehicle: Mapped[Vehicle] = relationship(back_populates="reservations", foreign_keys=[vehicle_id])
    connector: Mapped[Connector | None] = relationship(foreign_keys=[connector_id])
    # 多笔历史子预约可指向同一批次行；这里只做只读关联，不与 item 侧互设 back_populates
    fleet_batch_item: Mapped["FleetBatchItem | None"] = relationship(
        foreign_keys=[fleet_batch_item_id], viewonly=True
    )
    events: Mapped[list["ReservationEvent"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="ReservationEvent.id",
    )
    service_actions: Mapped[list["ServiceAction"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )


class ReservationEvent(Base):
    """订单生命周期事件流，所有转换与维护影响均留痕。"""

    __tablename__ = "reservation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="events")


class ServiceAction(Base):
    """客服动作：替代枪口/费用调整必须指向当时的订单与设备状态。"""

    __tablename__ = "service_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    kind: Mapped[ServiceActionKind] = mapped_column(Enum(ServiceActionKind))
    # 动作发生时的订单状态与设备状态快照（只读留痕）
    order_status_at_action: Mapped[ReservationStatus] = mapped_column(Enum(ReservationStatus))
    connector_state_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    target_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    amount: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="service_actions")
    target_connector: Mapped[Connector | None] = relationship(foreign_keys=[target_connector_id])


class IdempotencyRecord(Base):
    """幂等键 -> 首次请求的响应内容；重复提交原样返回。"""

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_path: Mapped[str] = mapped_column(String(128))
    response_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )


class FleetBatch(Base):
    """车队批次：一个车队编号一次出车安排，约定最低就绪数与确认路线。

    批次本身不直接持有容量，容量由其名下处于 HOLDING 状态的子预约持有；
    ready_at 非空表示已对外宣布“车队就绪”，之后任一子预约失效应立即重算，
    就绪数跌破最低线时清空 ready_at 并追加回退通知（已宣布可以撤回）。
    """

    __tablename__ = "fleet_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    fleet_no: Mapped[str] = mapped_column(String(32), unique=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    strategy: Mapped[FleetStrategy] = mapped_column(Enum(FleetStrategy))
    min_ready: Mapped[int] = mapped_column(Integer)
    status: Mapped[FleetBatchStatus] = mapped_column(
        Enum(FleetBatchStatus), default=FleetBatchStatus.OPEN
    )
    # 最近一次重算后的就绪计数与是否达到最低线（冗余落库，便于列表展示）
    ready_count: Mapped[int] = mapped_column(Integer, default=0)
    ready_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    site: Mapped[Site] = relationship()
    items: Mapped[list["FleetBatchItem"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="FleetBatchItem.position",
    )
    events: Mapped[list["FleetBatchEvent"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="FleetBatchEvent.id",
    )


class FleetBatchItem(Base):
    """批次内逐车需求行：接口、最低目标电量（kW）、到站范围、出发时刻。

    一行至多对应一笔子预约（reservation 唯一外键）；子预约取消后该行保留、
    reservation_id 置空并回到 PENDING，容量与就绪数通过批次重算对账。
    """

    __tablename__ = "fleet_batch_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("fleet_batches.id"))
    position: Mapped[int] = mapped_column(Integer)
    vehicle_label: Mapped[str] = mapped_column(String(64))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    connector_type: Mapped[str] = mapped_column(String(16))
    min_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible_required: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[ReservationMode] = mapped_column(
        Enum(ReservationMode), default=ReservationMode.CONNECTOR
    )
    arrival_start: Mapped[datetime] = mapped_column(DateTime)
    arrival_end: Mapped[datetime] = mapped_column(DateTime)
    departure_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[FleetItemStatus] = mapped_column(
        Enum(FleetItemStatus), default=FleetItemStatus.PENDING
    )
    # 普通列（不加 FK 约束以避免与 reservations.fleet_batch_item_id 形成循环外键）；
    # 取消后可重新预约，历史多笔子预约通过 reservations.fleet_batch_item_id 追溯到本行
    reservation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 逐车确认失败时留存结构化原因；成功或重算清空
    last_reason: Mapped[str] = mapped_column(Text, default="")
    # 子预约确认成功后签发，取消即作废；司机凭导航与进场码进场
    access_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    navigation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    batch: Mapped[FleetBatch] = relationship(back_populates="items")
    vehicle: Mapped[Vehicle | None] = relationship()
    # 当前子预约（reservation_id 只指向最新一笔；历史子预约经 Reservation.fleet_batch_item_id 追溯）
    reservation: Mapped["Reservation | None"] = relationship(
        primaryjoin="FleetBatchItem.reservation_id == Reservation.id",
        foreign_keys=[reservation_id],
        viewonly=True,
        uselist=False,
    )

    @property
    def reservation_ref(self) -> str | None:
        return self.reservation.ref if self.reservation is not None else None

    @property
    def reservation_status(self) -> str | None:
        return self.reservation.status.value if self.reservation is not None else None

    @property
    def connector_id(self) -> int | None:
        return self.reservation.connector_id if self.reservation is not None else None


class FleetBatchEvent(Base):
    """批次事件流：创建、逐车确认/失败、司机通知、车队就绪宣布与撤回，全程留痕可追溯。"""

    __tablename__ = "fleet_batch_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("fleet_batches.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    batch: Mapped[FleetBatch] = relationship(back_populates="events")
