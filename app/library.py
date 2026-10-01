"""標準步驟模組、變更範本，以及結案後把現場微調晉升為（變形）標準模組的審核流程。"""

from sqlalchemy import select

from app.audit import log
from app.auth import can
from app.changes import _require, _require_perm, clean, now
from app.models import (
    CONTENT_FIELDS,
    ChangeRequest,
    ChangeStatus,
    ChangeTemplate,
    DeviceType,
    ModuleStatus,
    PlanStep,
    ReviewDecision,
    SkipPolicy,
    StepModule,
    StepModuleRevision,
    TemplateItem,
)


def _content(values: dict) -> dict:
    return {f: clean(values.get(f)) for f in CONTENT_FIELDS}


def _code_taken(db, code) -> bool:
    return db.scalar(select(StepModule.id).filter_by(code=code)) is not None


def _new_module(db, user, *, code, title, device_type, status, parent_id=None, note="", content) -> StepModule:
    _require(clean(code) and clean(title), "代碼與名稱為必填")
    _require(not _code_taken(db, clean(code)), f"代碼 {clean(code)} 已存在")
    _require(device_type in (None, *DeviceType), "設備類型不正確")
    module = StepModule(code=clean(code), title=clean(title), device_type=device_type, status=status,
                        parent_id=parent_id, current_revision=1, created_by_id=user.id)
    module.revisions.append(StepModuleRevision(revision=1, note=clean(note), created_by_id=user.id, **content))
    db.add(module)
    db.flush()
    return module


def _new_revision(module: StepModule, user, note: str, content: dict):
    module.current_revision += 1
    module.revisions.append(StepModuleRevision(revision=module.current_revision, note=note, created_by_id=user.id,
                                               **content))


def create_module(db, user, *, code, title, device_type=None, note="", **content) -> StepModule:
    _require_perm(can(user, "module.edit"), "需要模組編輯角色")
    module = _new_module(db, user, code=code, title=title, device_type=device_type or None,
                         status=ModuleStatus.CANDIDATE, note=note, content=_content(content))
    log(db, user, "module.created", entity=module, code=module.code)
    return module


def revise_module(db, user, module: StepModule, *, note, **content):
    """修改模組 = 新增 Revision；舊 Revision 不可變，已使用的變更永遠指向當時的版本。"""
    _require(module.status != ModuleStatus.DEPRECATED, "已停用的模組不能修改")
    perm = "module.approve" if module.status == ModuleStatus.STANDARD else "module.edit"
    _require_perm(can(user, perm), "修改標準模組需要模組核准角色")
    _require(clean(note), "請填寫修訂說明")
    content = _content(content)
    _require(any(content[f] != getattr(module.current, f) for f in CONTENT_FIELDS), "內容沒有變更")
    _new_revision(module, user, clean(note), content)
    log(db, user, "module.revised", entity=module, code=module.code, revision=module.current_revision)


def set_module_status(db, user, module: StepModule, status):
    _require_perm(can(user, "module.approve"), "需要模組核准角色")
    _require(status in (ModuleStatus.STANDARD, ModuleStatus.DEPRECATED), "狀態不正確")
    before, module.status = module.status, status
    log(db, user, "module.status", entity=module, code=module.code, from_status=before, to_status=status)


def next_variant_code(db, base: str) -> str:
    n = 2
    while _code_taken(db, f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


# ---- 變更範本 ----


def create_template(db, user, *, code, name, description="") -> ChangeTemplate:
    _require_perm(can(user, "module.edit"), "需要模組編輯角色")
    _require(clean(code) and clean(name), "代碼與名稱為必填")
    _require(db.scalar(select(ChangeTemplate.id).filter_by(code=clean(code))) is None, "範本代碼已存在")
    tpl = ChangeTemplate(code=clean(code), name=clean(name), description=clean(description), created_by_id=user.id)
    db.add(tpl)
    db.flush()
    log(db, user, "template.created", entity=tpl, code=tpl.code)
    return tpl


def add_template_item(db, user, tpl: ChangeTemplate, *, module_id, skip_policy, condition_note=""):
    _require_perm(can(user, "module.edit"), "需要模組編輯角色")
    _require(skip_policy in list(SkipPolicy), "省略規則不正確")
    module = db.get(StepModule, module_id)
    _require(module and module.status == ModuleStatus.STANDARD, "範本只能使用標準模組")
    seq = max((i.seq for i in tpl.items), default=0) + 1
    tpl.items.append(TemplateItem(seq=seq, module=module, skip_policy=skip_policy,
                                  condition_note=clean(condition_note)))
    log(db, user, "template.item_added", entity=tpl, code=tpl.code, module=module.code, skip_policy=skip_policy)


def remove_template_item(db, user, tpl: ChangeTemplate, item_id: int):
    _require_perm(can(user, "module.edit"), "需要模組編輯角色")
    item = next((i for i in tpl.items if i.id == item_id), None)
    _require(item, "項目不存在")
    tpl.items.remove(item)
    log(db, user, "template.item_removed", entity=tpl, code=tpl.code, module=item.module.code)


def set_template_active(db, user, tpl: ChangeTemplate, active: bool):
    _require_perm(can(user, "module.edit"), "需要模組編輯角色")
    tpl.is_active = active
    log(db, user, "template.active", entity=tpl, code=tpl.code, active=active)


# ---- 結案後審核現場調整 ----


def pending_reviews(db) -> list[PlanStep]:
    return list(db.scalars(
        select(PlanStep).join(PlanStep.change)
        .where(PlanStep.is_modified, PlanStep.review_decision.is_(None), ChangeRequest.status == ChangeStatus.CLOSED)
        .order_by(PlanStep.id)
    ))


def review_step(db, user, step: PlanStep, decision, *, code="", title="", device_type=None):
    """
    KEEP_ONE_TIME：保留為該次變更的一次性調整。
    PROMOTE：來源為標準模組時建立變形模組（A01 → A01-2）；自訂步驟則建立新的標準模組（需指定代碼）。
    MERGE_PARENT：以調整後內容作為母模組的新 Revision。
    """
    _require_perm(can(user, "module.approve"), "需要模組核准角色")
    _require(step.is_modified and step.review_decision is None, "此步驟不需審核")
    _require(step.change.status == ChangeStatus.CLOSED, "變更結案後才能審核")
    _require(decision in list(ReviewDecision), "審核決定不正確")
    content = {f: getattr(step, f) for f in CONTENT_FIELDS}
    origin = f"{step.change.number} {step.code}：{step.modification_note}"
    parent = step.module
    if decision == ReviewDecision.MERGE_PARENT:
        _require(parent, "自訂步驟沒有母模組，請改用晉升")
        _require(parent.status == ModuleStatus.STANDARD, "母模組不是標準狀態")
        _require(parent.current_revision == step.module_revision,
                 "母模組在此變更之後已有新版本，請改用晉升為變形模組或手動修訂")
        _new_revision(parent, user, f"併入 {origin}", content)
        step.result_module = parent
    elif decision == ReviewDecision.PROMOTE:
        step.result_module = _new_module(
            db, user,
            code=next_variant_code(db, parent.code) if parent else code,
            title=title or step.title,
            device_type=parent.device_type if parent else (device_type or None),
            status=ModuleStatus.STANDARD,
            parent_id=parent.id if parent else None,
            note=f"由 {origin} 晉升",
            content=content,
        )
    step.review_decision, step.reviewed_by_id, step.reviewed_at = decision, user.id, now()
    log(db, user, "module.review", change=step.change, entity=step, code=step.code, decision=decision,
        result_module=step.result_module.code if step.result_module else None)
