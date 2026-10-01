import difflib

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import library
from app.db import get_db
from app.models import (
    CONTENT_FIELDS,
    ChangeTemplate,
    DeviceType,
    ModuleStatus,
    PlanStep,
    ReviewDecision,
    SkipPolicy,
    StepModule,
    StepModuleRevision,
    User,
)
from app.web.common import current_user, get_or_404, redirect, render, verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/modules")
def module_list(request: Request, status: str = "", user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    q = select(StepModule).order_by(StepModule.code)
    if status:
        q = q.filter_by(status=status)
    return render(request, "modules.html", user, modules=db.scalars(q).all(), status=status,
                  statuses=list(ModuleStatus), device_types=list(DeviceType))


@router.post("/modules")
def module_create(code: str = Form(...), title: str = Form(...), device_type: str = Form(""),
                  note: str = Form(""), instruction: str = Form(""), command: str = Form(""),
                  verification: str = Form(""), rollback: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = library.create_module(db, user, code=code, title=title, device_type=device_type or None, note=note,
                              instruction=instruction, command=command, verification=verification,
                              rollback=rollback)
    db.commit()
    return redirect(f"/modules/{m.id}")


@router.get("/modules/{mid}")
def module_detail(mid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = get_or_404(db, StepModule, mid)
    variants = db.scalars(select(StepModule).filter_by(parent_id=m.id).order_by(StepModule.code)).all()
    usage = db.scalar(select(func.count(PlanStep.id)).filter_by(module_id=m.id))
    return render(request, "module_detail.html", user, m=m, variants=variants, usage=usage,
                  revisions=list(reversed(m.revisions)))


@router.post("/modules/{mid}/revise")
def module_revise(mid: int, note: str = Form(""), instruction: str = Form(""), command: str = Form(""),
                  verification: str = Form(""), rollback: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    library.revise_module(db, user, get_or_404(db, StepModule, mid), note=note, instruction=instruction,
                          command=command, verification=verification, rollback=rollback)
    db.commit()
    return redirect(f"/modules/{mid}")


@router.post("/modules/{mid}/status")
def module_status(mid: int, status: str = Form(...), user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    library.set_module_status(db, user, get_or_404(db, StepModule, mid), status)
    db.commit()
    return redirect(f"/modules/{mid}")


# ---- 變更範本 ----


@router.get("/change-templates")
def template_list(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpls = db.scalars(select(ChangeTemplate).order_by(ChangeTemplate.code)).all()
    return render(request, "change_templates.html", user, templates=tpls)


@router.post("/change-templates")
def template_create(code: str = Form(...), name: str = Form(...), description: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpl = library.create_template(db, user, code=code, name=name, description=description)
    db.commit()
    return redirect(f"/change-templates/{tpl.id}")


@router.get("/change-templates/{tid}")
def template_detail(tid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpl = get_or_404(db, ChangeTemplate, tid)
    modules = db.scalars(select(StepModule).filter_by(status=ModuleStatus.STANDARD).order_by(StepModule.code)).all()
    return render(request, "change_template_detail.html", user, tpl=tpl, modules=modules,
                  skip_policies=list(SkipPolicy))


@router.post("/change-templates/{tid}/items")
def template_item_add(tid: int, module_id: int = Form(...), skip_policy: str = Form(...),
                      condition_note: str = Form(""), user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    library.add_template_item(db, user, get_or_404(db, ChangeTemplate, tid), module_id=module_id,
                              skip_policy=skip_policy, condition_note=condition_note)
    db.commit()
    return redirect(f"/change-templates/{tid}")


@router.post("/change-templates/{tid}/items/{iid}/delete")
def template_item_remove(tid: int, iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    library.remove_template_item(db, user, get_or_404(db, ChangeTemplate, tid), iid)
    db.commit()
    return redirect(f"/change-templates/{tid}")


@router.post("/change-templates/{tid}/active")
def template_active(tid: int, active: bool = Form(False), user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    library.set_template_active(db, user, get_or_404(db, ChangeTemplate, tid), active)
    db.commit()
    return redirect(f"/change-templates/{tid}")


# ---- 結案後審核 ----


def _diff(step: PlanStep, db) -> str:
    if not step.module_id:
        return ""
    rev = db.scalar(select(StepModuleRevision).filter_by(module_id=step.module_id, revision=step.module_revision))
    lines = []
    for f in CONTENT_FIELDS:
        lines += difflib.unified_diff(getattr(rev, f).splitlines(), getattr(step, f).splitlines(),
                                      f"{f} ({step.code} r{step.module_revision})", f"{f} (本次)", lineterm="")
    return "\n".join(lines)


@router.get("/reviews")
def review_list(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    steps = library.pending_reviews(db)
    return render(request, "reviews.html", user, items=[(s, _diff(s, db)) for s in steps],
                  decisions=list(ReviewDecision), device_types=list(DeviceType))


@router.post("/reviews/{sid}")
def review_submit(sid: int, decision: str = Form(...), code: str = Form(""), title: str = Form(""),
                  device_type: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    library.review_step(db, user, get_or_404(db, PlanStep, sid), decision, code=code, title=title,
                        device_type=device_type or None)
    db.commit()
    return redirect("/reviews")
