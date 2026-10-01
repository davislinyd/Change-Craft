"""變更請求的計畫、狀態機與執行。所有寫入都透過這裡，以確保規則與日誌一致。"""

from datetime import datetime, timezone

from sqlalchemy import select

from app import config
from app.audit import log
from app.auth import can
from app.errors import DomainError, PermissionDenied
from app.models import (
    CONTENT_FIELDS,
    ChangeRequest,
    ChangeResult,
    ChangeStatus,
    ChangeTarget,
    ChangeTemplate,
    Device,
    ModuleStatus,
    PlanStep,
    Risk,
    SkipPolicy,
    StepExecution,
    StepModule,
    StepModuleRevision,
    StepStatus,
)

S = ChangeStatus

# action → (允許的來源狀態, 目標狀態)
TRANSITIONS = {
    "submit": ({S.DRAFT}, S.SUBMITTED),
    "approve": ({S.SUBMITTED}, S.APPROVED),
    "reject": ({S.SUBMITTED}, S.DRAFT),
    "start": ({S.APPROVED}, S.IN_PROGRESS),
    "pause": ({S.IN_PROGRESS}, S.PAUSED),
    "resume": ({S.PAUSED}, S.IN_PROGRESS),
    "finish": ({S.IN_PROGRESS}, S.EXECUTED),
    "cancel": ({S.DRAFT, S.SUBMITTED, S.APPROVED}, S.CANCELLED),
}
# 聊天只能更新基礎狀態；計畫、核准、步驟細節與結案只能在網頁完成。
CHAT_ACTIONS = {"start", "pause", "resume", "finish"}


def now():
    return datetime.now(timezone.utc)


def clean(text: str | None) -> str:
    """統一換行與前後空白，避免瀏覽器送出的 \\r\\n 被誤判為偏離標準模組。"""
    return (text or "").replace("\r\n", "\n").strip()


def _require(cond, msg):
    if not cond:
        raise DomainError(msg)


def _require_perm(cond, msg="權限不足"):
    if not cond:
        raise PermissionDenied(msg)


def _require_draft_owner(cr: ChangeRequest, user):
    _require(cr.status == S.DRAFT, "只有草稿狀態可以修改計畫")
    _require_perm(cr.requester_id == user.id, "只有申請人可以修改此變更")


def _applies(device_type: str | None, target: ChangeTarget) -> bool:
    return device_type is None or device_type == target.device_type


def _validate_window(start, end):
    _require(start is None or end is None or end > start, "結束時間必須晚於開始時間")


# ---- 建立與編輯 ----


def create_change(db, user, *, title, purpose, reason="", change_items="", risk=Risk.MEDIUM,
                  window_start=None, window_end=None, template_id=None) -> ChangeRequest:
    _require_perm(can(user, "change.create"), "需要申請人角色")
    _require(clean(title) and clean(purpose), "標題與目的為必填")
    _require(risk in list(Risk), "風險等級不正確")
    _validate_window(window_start, window_end)
    cr = ChangeRequest(
        title=clean(title), purpose=clean(purpose), reason=clean(reason), change_items=clean(change_items),
        risk=risk, window_start=window_start, window_end=window_end, requester_id=user.id,
    )
    db.add(cr)
    db.flush()
    cr.number = f"CR-{now().astimezone(config.TZ):%Y%m%d}-{cr.id:04d}"
    if template_id:
        tpl = db.get(ChangeTemplate, template_id)
        _require(tpl and tpl.is_active, "變更範本不存在或已停用")
        cr.template_id = tpl.id
        for item in tpl.items:
            _add_module_step(cr, item.module, item.skip_policy, item.condition_note)
    db.flush()
    log(db, user, "change.created", change=cr, entity=cr, template=cr.template.code if cr.template else None)
    return cr


def update_change(db, user, cr, *, title, purpose, reason, change_items, risk, window_start, window_end):
    _require_draft_owner(cr, user)
    _require(clean(title) and clean(purpose), "標題與目的為必填")
    _require(risk in list(Risk), "風險等級不正確")
    _validate_window(window_start, window_end)
    cr.title, cr.purpose, cr.reason, cr.change_items = clean(title), clean(purpose), clean(reason), clean(change_items)
    cr.risk, cr.window_start, cr.window_end = risk, window_start, window_end
    log(db, user, "change.updated", change=cr, entity=cr)


def add_target(db, user, cr, device_id: int) -> ChangeTarget:
    _require_draft_owner(cr, user)
    dev = db.get(Device, device_id)
    _require(dev and dev.is_active, "設備不存在或已停用")
    _require(all(t.device_id != dev.id for t in cr.targets), "此設備已在變更中")
    target = ChangeTarget(device=dev, hostname=dev.hostname, ip=dev.ip, device_type=dev.device_type,
                          environment=dev.environment)
    cr.targets.append(target)
    for step in cr.steps:
        if _applies(step.module.device_type if step.module else None, target):
            step.executions.append(StepExecution(target=target))
    db.flush()
    log(db, user, "target.added", change=cr, entity=target, hostname=target.hostname)
    return target


def remove_target(db, user, cr, target_id: int):
    _require_draft_owner(cr, user)
    target = next((t for t in cr.targets if t.id == target_id), None)
    _require(target, "設備不在此變更中")
    log(db, user, "target.removed", change=cr, entity=target, hostname=target.hostname)
    db.delete(target)
    db.flush()
    db.expire_all()


def _next_seq(cr) -> int:
    return max((s.seq for s in cr.steps), default=0) + 1


def _add_module_step(cr, module: StepModule, skip_policy, condition_note="") -> PlanStep:
    rev = module.current
    step = PlanStep(
        seq=_next_seq(cr), module=module, module_revision=rev.revision, code=module.code, title=module.title,
        skip_policy=skip_policy, condition_note=clean(condition_note),
        **{f: getattr(rev, f) for f in CONTENT_FIELDS},
    )
    cr.steps.append(step)
    for target in cr.targets:
        if _applies(module.device_type, target):
            step.executions.append(StepExecution(target=target))
    return step


def add_module_step(db, user, cr, module_id: int, skip_policy, condition_note="") -> PlanStep:
    _require_draft_owner(cr, user)
    _require(skip_policy in list(SkipPolicy), "省略規則不正確")
    module = db.get(StepModule, module_id)
    _require(module and module.status == ModuleStatus.STANDARD, "只能使用標準模組")
    step = _add_module_step(cr, module, skip_policy, condition_note)
    db.flush()
    log(db, user, "step.added", change=cr, entity=step, code=step.code, revision=step.module_revision)
    return step


def add_custom_step(db, user, cr, *, title, skip_policy, condition_note="", modification_note, **content) -> PlanStep:
    _require_draft_owner(cr, user)
    _require(skip_policy in list(SkipPolicy), "省略規則不正確")
    _require(clean(title), "步驟名稱為必填")
    _require(clean(modification_note), "自訂步驟必須說明原因")
    step = PlanStep(
        seq=_next_seq(cr), code="CUSTOM", title=clean(title), skip_policy=skip_policy,
        condition_note=clean(condition_note), is_modified=True, modification_note=clean(modification_note),
        **{f: clean(content.get(f)) for f in CONTENT_FIELDS},
    )
    cr.steps.append(step)
    for target in cr.targets:
        step.executions.append(StepExecution(target=target))
    db.flush()
    log(db, user, "step.added", change=cr, entity=step, code=step.code, title=step.title)
    return step


def update_step(db, user, step: PlanStep, *, seq, skip_policy, condition_note="", modification_note="", **content):
    cr = step.change
    _require_draft_owner(cr, user)
    _require(skip_policy in list(SkipPolicy), "省略規則不正確")
    for f in CONTENT_FIELDS:
        setattr(step, f, clean(content.get(f)))
    step.seq, step.skip_policy = seq, skip_policy
    step.condition_note, step.modification_note = clean(condition_note), clean(modification_note)
    if step.module_id:
        rev = db.scalar(select(StepModuleRevision).filter_by(module_id=step.module_id, revision=step.module_revision))
        step.is_modified = any(getattr(step, f) != getattr(rev, f) for f in CONTENT_FIELDS)
    _require(not step.is_modified or step.modification_note, "內容與標準模組不同，必須填寫調整原因")
    log(db, user, "step.updated", change=cr, entity=step, code=step.code, modified=step.is_modified)


def remove_step(db, user, step: PlanStep):
    cr = step.change
    _require_draft_owner(cr, user)
    log(db, user, "step.removed", change=cr, entity=step, code=step.code)
    db.delete(step)
    db.flush()
    db.expire_all()


def toggle_applicability(db, user, step: PlanStep, target_id: int):
    """切換步驟是否掛在某設備上。"""
    cr = step.change
    _require_draft_owner(cr, user)
    target = next((t for t in cr.targets if t.id == target_id), None)
    _require(target, "設備不在此變更中")
    existing = next((e for e in step.executions if e.target_id == target_id), None)
    if existing:
        db.delete(existing)
    else:
        step.executions.append(StepExecution(target=target))
    log(db, user, "step.applicability", change=cr, entity=step, code=step.code, hostname=target.hostname,
        applies=existing is None)
    db.flush()
    db.expire_all()


# ---- 狀態機 ----


def _can_approve(user, cr) -> bool:
    return all(can(user, "change.approve", t) for t in cr.targets)


def _can_execute(user, cr) -> bool:
    return any(can(user, "change.execute", t) for t in cr.targets)


def _guard_submit(user, cr, comment):
    _require_perm(cr.requester_id == user.id, "只有申請人可以送審")
    _require(cr.window_start and cr.window_end, "請設定變更時段")
    _require(cr.targets, "至少需要一台設備")
    _require(cr.steps, "至少需要一個步驟")
    _require(all(s.executions for s in cr.steps), "每個步驟都必須掛到至少一台設備")
    _require(all(t.executions for t in cr.targets), "每台設備都必須至少有一個步驟")


def _guard_review(user, cr, comment):
    _require_perm(cr.requester_id != user.id, "申請人不能核准自己的變更")
    _require_perm(_can_approve(user, cr), "核准範圍未涵蓋所有設備")


def _guard_reject(user, cr, comment):
    _guard_review(user, cr, comment)
    _require(comment, "退回必須填寫原因")


def _guard_start(user, cr, comment):
    _require_perm(_can_execute(user, cr), "沒有此變更任何設備的執行權限")
    t = now()
    _require(cr.window_start <= t <= cr.window_end, "不在核准的變更時段內")


def _guard_execute(user, cr, comment):
    _require_perm(_can_execute(user, cr), "沒有此變更任何設備的執行權限")


def _guard_finish(user, cr, comment):
    _guard_execute(user, cr, comment)
    pending = sum(1 for s in cr.steps for e in s.executions if e.status == StepStatus.PENDING)
    _require(not pending, f"尚有 {pending} 個步驟未處理，請先在網頁更新步驟狀態")


def _guard_cancel(user, cr, comment):
    _require_perm(cr.requester_id == user.id, "只有申請人可以取消")
    _require(comment, "取消必須填寫原因")


GUARDS = {
    "submit": _guard_submit,
    "approve": _guard_review,
    "reject": _guard_reject,
    "start": _guard_start,
    "pause": _guard_execute,
    "resume": _guard_execute,
    "finish": _guard_finish,
    "cancel": _guard_cancel,
}


def allowed_actions(cr) -> list[str]:
    return [a for a, (src, _) in TRANSITIONS.items() if cr.status in src]


def transition(db, user, cr, action: str, *, source="web", comment=""):
    _require(action in TRANSITIONS, "未知的動作")
    _require(source != "chat" or action in CHAT_ACTIONS, "此動作只能在網頁上執行")
    allowed_from, target_status = TRANSITIONS[action]
    _require(cr.status in allowed_from, f"目前狀態 {cr.status} 不能執行 {action}")
    comment = clean(comment)
    GUARDS[action](user, cr, comment)
    before, cr.status = cr.status, target_status
    if action == "approve":
        cr.approved_by_id, cr.approved_at = user.id, now()
    elif action == "start" and not cr.started_at:
        cr.started_at = now()
    elif action == "finish":
        cr.finished_at = now()
    log(db, user, f"change.{action}", change=cr, entity=cr, source=source, from_status=before,
        to_status=target_status, comment=comment)


# ---- 執行與結案 ----


def update_execution(db, user, ex: StepExecution, *, status, note="", output=""):
    step, target = ex.plan_step, ex.target
    cr = step.change
    _require(cr.status == S.IN_PROGRESS, "變更不在執行中")
    _require_perm(can(user, "change.execute", target), f"沒有 {target.hostname} 的執行權限")
    _require(status in list(StepStatus), "步驟狀態不正確")
    note = clean(note)
    if status == StepStatus.SKIPPED:
        _require(step.skip_policy != SkipPolicy.REQUIRED, "必要步驟不可省略")
        _require(step.skip_policy == SkipPolicy.OPTIONAL or note, "條件式步驟省略時必須填寫原因")
    before = ex.status
    ex.status, ex.note, ex.output = status, note, clean(output)
    ex.executed_by_id, ex.executed_at = user.id, now()
    log(db, user, "step.executed", change=cr, entity=ex, code=step.code, hostname=target.hostname,
        from_status=before, to_status=status, note=note)


def record_and_close(db, user, cr, *, result, impact="", issues="", rollback_used=False, verification_result,
                     notes=""):
    """執行完畢後回到主變更回填紀錄；這是結案的唯一路徑。"""
    _require(cr.status == S.EXECUTED, "變更尚未執行完畢")
    _require_perm(cr.requester_id == user.id, "只有申請人可以回填變更紀錄")
    _require(result in list(ChangeResult), "請選擇變更結果")
    _require(clean(verification_result), "請填寫驗證結果")
    cr.result, cr.result_impact, cr.result_issues = result, clean(impact), clean(issues)
    cr.rollback_used, cr.verification_result, cr.result_notes = rollback_used, clean(verification_result), clean(notes)
    cr.status, cr.closed_by_id, cr.closed_at = S.CLOSED, user.id, now()
    log(db, user, "change.closed", change=cr, entity=cr, result=result, rollback_used=rollback_used)


def add_comment(db, user, cr, text: str, *, source="web"):
    _require(clean(text), "留言不可空白")
    log(db, user, "change.comment", change=cr, entity=cr, source=source, comment=clean(text))
