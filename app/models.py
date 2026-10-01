"""領域模型。

- Device 不保存任何變更資料；設備與變更的關聯只存在於 ChangeTarget，
  所以同一張變更的多台設備互不知道彼此，只能透過主變更查詢。
- StepModule（樂高積木）有不可變的 Revision；ChangeTemplate（變更目的）把模組組裝起來並定義可否省略。
- PlanStep 是模組在單一變更中的快照（可微調），StepExecution 是該步驟在單一設備上的執行結果。
- Event 為 append-only，資料庫 trigger 禁止 UPDATE/DELETE。
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(StrEnum):
    REQUESTER = "REQUESTER"
    APPROVER = "APPROVER"
    OPERATOR = "OPERATOR"
    MODULE_EDITOR = "MODULE_EDITOR"
    MODULE_APPROVER = "MODULE_APPROVER"
    AUDITOR = "AUDITOR"
    ADMIN = "ADMIN"


class DeviceType(StrEnum):
    NETWORK = "NETWORK"
    WINDOWS = "WINDOWS"
    LINUX = "LINUX"
    OTHER = "OTHER"


class ChangeStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    EXECUTED = "EXECUTED"  # 執行完畢，必須回到網頁回填變更紀錄才能結案
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ChangeResult(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SkipPolicy(StrEnum):
    REQUIRED = "REQUIRED"  # 不可省略
    OPTIONAL = "OPTIONAL"  # 可省略，不需原因
    CONDITIONAL = "CONDITIONAL"  # 可省略，但必須填寫原因


class StepStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ROLLED_BACK = "ROLLED_BACK"


class ModuleStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    STANDARD = "STANDARD"
    DEPRECATED = "DEPRECATED"


class ReviewDecision(StrEnum):
    KEEP_ONE_TIME = "KEEP_ONE_TIME"
    PROMOTE = "PROMOTE"
    MERGE_PARENT = "MERGE_PARENT"


# 模組 Revision 與 PlanStep 共用的步驟內容欄位；用來判斷 PlanStep 是否偏離標準模組。
CONTENT_FIELDS = ("instruction", "command", "verification", "rollback")


def _created_at():
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    chat_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created_at()

    roles: Mapped[list["RoleAssignment"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class RoleAssignment(Base):
    """RBAC + 資源範圍：device_type / environment 為 NULL 代表不限。"""

    __tablename__ = "role_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    device_type: Mapped[str | None] = mapped_column(String(16))
    environment: Mapped[str | None] = mapped_column(String(64))


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True)
    device_type: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(INET)
    vendor: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    environment: Mapped[str] = mapped_column(String(64), default="")
    owner_team: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StepModule(Base):
    """標準步驟模組（樂高積木），例如 A01；變形模組以 parent 指回母模組，例如 A01-2。"""

    __tablename__ = "step_modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    # 適用設備類型；NULL 代表所有類型。加入變更時只自動掛到相符的設備。
    device_type: Mapped[str | None] = mapped_column(String(16))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("step_modules.id"))
    status: Mapped[str] = mapped_column(String(16), default=ModuleStatus.CANDIDATE)
    current_revision: Mapped[int] = mapped_column(default=1)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created_at()

    parent: Mapped["StepModule | None"] = relationship(remote_side=[id])
    revisions: Mapped[list["StepModuleRevision"]] = relationship(order_by="StepModuleRevision.revision")

    @property
    def current(self) -> "StepModuleRevision":
        return next(r for r in self.revisions if r.revision == self.current_revision)


class StepModuleRevision(Base):
    """不可變；資料庫 trigger 禁止 UPDATE/DELETE。"""

    __tablename__ = "step_module_revisions"
    __table_args__ = (UniqueConstraint("module_id", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("step_modules.id"))
    revision: Mapped[int]
    instruction: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(Text, default="")
    rollback: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created_at()

    created_by: Mapped["User | None"] = relationship()


class ChangeTemplate(Base):
    """變更目的（例如 A）：由多個標準模組組成，並定義每個步驟可否省略。"""

    __tablename__ = "change_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created_at()

    items: Mapped[list["TemplateItem"]] = relationship(cascade="all, delete-orphan", order_by="TemplateItem.seq")


class TemplateItem(Base):
    __tablename__ = "template_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("change_templates.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int]
    module_id: Mapped[int] = mapped_column(ForeignKey("step_modules.id"))
    # 可否省略屬於「組裝」而非模組本身：同一模組在不同變更目的中可以有不同的省略規則。
    skip_policy: Mapped[str] = mapped_column(String(16))
    condition_note: Mapped[str] = mapped_column(Text, default="")

    module: Mapped["StepModule"] = relationship(lazy="joined")


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    change_items: Mapped[str] = mapped_column(String(512), default="")
    risk: Mapped[str] = mapped_column(String(16), default=Risk.MEDIUM)
    status: Mapped[str] = mapped_column(String(16), default=ChangeStatus.DRAFT, index=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("change_templates.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 變更紀錄（執行完畢後回填）
    result: Mapped[str | None] = mapped_column(String(16))
    result_impact: Mapped[str] = mapped_column(Text, default="")
    result_issues: Mapped[str] = mapped_column(Text, default="")
    rollback_used: Mapped[bool] = mapped_column(default=False)
    verification_result: Mapped[str] = mapped_column(Text, default="")
    result_notes: Mapped[str] = mapped_column(Text, default="")
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    requester: Mapped["User"] = relationship(foreign_keys=[requester_id])
    approved_by: Mapped["User | None"] = relationship(foreign_keys=[approved_by_id])
    closed_by: Mapped["User | None"] = relationship(foreign_keys=[closed_by_id])
    template: Mapped["ChangeTemplate | None"] = relationship()
    targets: Mapped[list["ChangeTarget"]] = relationship(
        back_populates="change", cascade="all, delete-orphan", order_by="ChangeTarget.id"
    )
    steps: Mapped[list["PlanStep"]] = relationship(
        back_populates="change", cascade="all, delete-orphan", order_by=lambda: (PlanStep.seq, PlanStep.id)
    )


class ChangeTarget(Base):
    """變更與設備的唯一關聯點。保存設備在變更當下的快照，設備日後改 IP 仍可用舊 IP 搜到。"""

    __tablename__ = "change_targets"
    __table_args__ = (UniqueConstraint("change_id", "device_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("change_requests.id", ondelete="CASCADE"))
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(INET)
    device_type: Mapped[str] = mapped_column(String(16))
    environment: Mapped[str] = mapped_column(String(64), default="")

    change: Mapped["ChangeRequest"] = relationship(back_populates="targets")
    device: Mapped["Device"] = relationship()
    executions: Mapped[list["StepExecution"]] = relationship(back_populates="target", cascade="all, delete-orphan")


class PlanStep(Base):
    __tablename__ = "plan_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("change_requests.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int]
    # 來源模組與版本；自訂步驟為 NULL。
    module_id: Mapped[int | None] = mapped_column(ForeignKey("step_modules.id"))
    module_revision: Mapped[int | None]
    code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    instruction: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(Text, default="")
    rollback: Mapped[str] = mapped_column(Text, default="")
    skip_policy: Mapped[str] = mapped_column(String(16), default=SkipPolicy.REQUIRED)
    condition_note: Mapped[str] = mapped_column(Text, default="")
    # 內容與來源 Revision 不同（或為自訂步驟）時為 True，結案後進入模組審核。
    is_modified: Mapped[bool] = mapped_column(default=False)
    modification_note: Mapped[str] = mapped_column(Text, default="")
    review_decision: Mapped[str | None] = mapped_column(String(16))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_module_id: Mapped[int | None] = mapped_column(ForeignKey("step_modules.id"))

    change: Mapped["ChangeRequest"] = relationship(back_populates="steps")
    module: Mapped["StepModule | None"] = relationship(foreign_keys=[module_id])
    result_module: Mapped["StepModule | None"] = relationship(foreign_keys=[result_module_id])
    executions: Mapped[list["StepExecution"]] = relationship(back_populates="plan_step", cascade="all, delete-orphan")


class StepExecution(Base):
    """步驟與設備的掛勾：存在即代表此步驟要在此設備上執行。"""

    __tablename__ = "step_executions"
    __table_args__ = (UniqueConstraint("plan_step_id", "target_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_step_id: Mapped[int] = mapped_column(ForeignKey("plan_steps.id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(ForeignKey("change_targets.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=StepStatus.PENDING)
    note: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    executed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    plan_step: Mapped["PlanStep"] = relationship(back_populates="executions")
    target: Mapped["ChangeTarget"] = relationship(back_populates="executions")
    executed_by: Mapped["User | None"] = relationship()


class Event(Base):
    """Append-only 操作日誌：變更時間軸、步驟執行紀錄與系統稽核共用。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    change_id: Mapped[int | None] = mapped_column(ForeignKey("change_requests.id"), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None]
    action: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16), default="web")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created_at()

    actor: Mapped["User | None"] = relationship()
