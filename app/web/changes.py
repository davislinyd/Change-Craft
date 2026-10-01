from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import changes, library
from app.auth import can
from app.db import get_db
from app.errors import DomainError
from app.models import (
    ChangeRequest,
    ChangeResult,
    ChangeStatus,
    ChangeTemplate,
    Device,
    Event,
    ModuleStatus,
    PlanStep,
    Risk,
    SkipPolicy,
    StepExecution,
    StepModule,
    StepStatus,
    User,
)
from app.search import search_changes
from app.web.common import current_user, get_or_404, parse_date, parse_dt, redirect, render, verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])
S = ChangeStatus


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(ChangeRequest).order_by(ChangeRequest.window_start.nulls_last(), ChangeRequest.id)
    mine = db.scalars(q.where(ChangeRequest.requester_id == user.id,
                              ChangeRequest.status.not_in([S.CLOSED, S.CANCELLED]))).all()
    to_approve = []
    if can(user, "change.approve"):
        to_approve = db.scalars(q.where(ChangeRequest.status == S.SUBMITTED,
                                        ChangeRequest.requester_id != user.id)).all()
    active = db.scalars(q.where(ChangeRequest.status.in_([S.APPROVED, S.IN_PROGRESS, S.PAUSED]))).all()
    reviews = len(library.pending_reviews(db)) if can(user, "module.approve") else 0
    return render(request, "dashboard.html", user, mine=mine, to_approve=to_approve, active=active, reviews=reviews)


@router.get("/changes")
def change_list(request: Request, q: str = "", hostname: str = "", ip: str = "", item: str = "", purpose: str = "",
                status: str = "", date_from: str = "", date_to: str = "",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    params = dict(q=q, hostname=hostname, ip=ip, item=item, purpose=purpose, status=status, date_from=date_from,
                  date_to=date_to)
    try:
        results, error = search_changes(db, q=q, hostname=hostname, ip=ip, item=item, purpose=purpose, status=status,
                                        date_from=parse_date(date_from), date_to=parse_date(date_to, end=True)), None
    except DomainError as e:  # GET 不能用 flash + 導回，否則重新整理會無限導向
        results, error = [], str(e)
    return render(request, "changes.html", user, results=results, params=params, statuses=list(S), error=error)


@router.get("/changes/new")
def change_new(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpls = db.scalars(select(ChangeTemplate).filter_by(is_active=True).order_by(ChangeTemplate.code)).all()
    return render(request, "change_new.html", user, templates=tpls, risks=list(Risk))


@router.post("/changes")
def change_create(title: str = Form(...), purpose: str = Form(...), reason: str = Form(""),
                  change_items: str = Form(""), risk: str = Form(Risk.MEDIUM), window_start: str = Form(""),
                  window_end: str = Form(""), template_id: int | None = Form(None),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    cr = changes.create_change(db, user, title=title, purpose=purpose, reason=reason, change_items=change_items,
                               risk=risk, window_start=parse_dt(window_start), window_end=parse_dt(window_end),
                               template_id=template_id)
    db.commit()
    return redirect(f"/changes/{cr.id}")


@router.get("/changes/{cid}")
def change_detail(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cr = get_or_404(db, ChangeRequest, cid)
    editable = cr.status == S.DRAFT and cr.requester_id == user.id
    ctx = dict(
        cr=cr,
        editable=editable,
        actions=changes.allowed_actions(cr),
        matrix={s.id: {e.target_id: e for e in s.executions} for s in cr.steps},
        events=db.scalars(select(Event).filter_by(change_id=cr.id).order_by(Event.id)).all(),
        step_statuses=list(StepStatus),
        skip_policies=list(SkipPolicy),
        results=list(ChangeResult),
        risks=list(Risk),
    )
    if editable:
        used = {t.device_id for t in cr.targets}
        ctx["devices"] = [d for d in db.scalars(select(Device).filter_by(is_active=True).order_by(Device.hostname))
                          if d.id not in used]
        ctx["modules"] = db.scalars(select(StepModule).filter_by(status=ModuleStatus.STANDARD)
                                    .order_by(StepModule.code)).all()
    return render(request, "change_detail.html", user, **ctx)


@router.post("/changes/{cid}/edit")
def change_edit(cid: int, title: str = Form(...), purpose: str = Form(...), reason: str = Form(""),
                change_items: str = Form(""), risk: str = Form(...), window_start: str = Form(""),
                window_end: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    cr = get_or_404(db, ChangeRequest, cid)
    changes.update_change(db, user, cr, title=title, purpose=purpose, reason=reason, change_items=change_items,
                          risk=risk, window_start=parse_dt(window_start), window_end=parse_dt(window_end))
    db.commit()
    return redirect(f"/changes/{cid}")


@router.post("/changes/{cid}/targets")
def target_add(cid: int, device_id: int = Form(...), user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    changes.add_target(db, user, get_or_404(db, ChangeRequest, cid), device_id)
    db.commit()
    return redirect(f"/changes/{cid}#targets")


@router.post("/changes/{cid}/targets/{tid}/delete")
def target_remove(cid: int, tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.remove_target(db, user, get_or_404(db, ChangeRequest, cid), tid)
    db.commit()
    return redirect(f"/changes/{cid}#targets")


@router.post("/changes/{cid}/steps")
def step_add(cid: int, module_id: int = Form(...), skip_policy: str = Form(...), condition_note: str = Form(""),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.add_module_step(db, user, get_or_404(db, ChangeRequest, cid), module_id, skip_policy, condition_note)
    db.commit()
    return redirect(f"/changes/{cid}#plan")


@router.post("/changes/{cid}/steps/custom")
def step_add_custom(cid: int, title: str = Form(...), skip_policy: str = Form(...), condition_note: str = Form(""),
                    modification_note: str = Form(""), instruction: str = Form(""), command: str = Form(""),
                    verification: str = Form(""), rollback: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.add_custom_step(db, user, get_or_404(db, ChangeRequest, cid), title=title, skip_policy=skip_policy,
                            condition_note=condition_note, modification_note=modification_note,
                            instruction=instruction, command=command, verification=verification, rollback=rollback)
    db.commit()
    return redirect(f"/changes/{cid}#plan")


def _step(db, cid, sid) -> PlanStep:
    step = get_or_404(db, PlanStep, sid)
    if step.change_id != cid:
        raise HTTPException(404, "找不到資料")
    return step


@router.post("/changes/{cid}/steps/{sid}")
def step_update(cid: int, sid: int, seq: int = Form(...), skip_policy: str = Form(...),
                condition_note: str = Form(""), modification_note: str = Form(""), instruction: str = Form(""),
                command: str = Form(""), verification: str = Form(""), rollback: str = Form(""),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.update_step(db, user, _step(db, cid, sid), seq=seq, skip_policy=skip_policy,
                        condition_note=condition_note, modification_note=modification_note,
                        instruction=instruction, command=command, verification=verification, rollback=rollback)
    db.commit()
    return redirect(f"/changes/{cid}#step-{sid}")


@router.post("/changes/{cid}/steps/{sid}/delete")
def step_remove(cid: int, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.remove_step(db, user, _step(db, cid, sid))
    db.commit()
    return redirect(f"/changes/{cid}#plan")


@router.post("/changes/{cid}/steps/{sid}/targets/{tid}")
def step_toggle(cid: int, sid: int, tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.toggle_applicability(db, user, _step(db, cid, sid), tid)
    db.commit()
    return redirect(f"/changes/{cid}#step-{sid}")


@router.post("/changes/{cid}/action")
def change_action(cid: int, action: str = Form(...), comment: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.transition(db, user, get_or_404(db, ChangeRequest, cid), action, comment=comment)
    db.commit()
    return redirect(f"/changes/{cid}")


@router.post("/changes/{cid}/executions/{eid}")
def execution_update(cid: int, eid: int, status: str = Form(...), note: str = Form(""), output: str = Form(""),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    ex = get_or_404(db, StepExecution, eid)
    if ex.plan_step.change_id != cid:
        raise HTTPException(404, "找不到資料")
    changes.update_execution(db, user, ex, status=status, note=note, output=output)
    db.commit()
    return redirect(f"/changes/{cid}#step-{ex.plan_step_id}")


@router.post("/changes/{cid}/record")
def change_record(cid: int, result: str = Form(""), impact: str = Form(""), issues: str = Form(""),
                  rollback_used: bool = Form(False), verification_result: str = Form(""), notes: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    changes.record_and_close(db, user, get_or_404(db, ChangeRequest, cid), result=result, impact=impact,
                             issues=issues, rollback_used=rollback_used, verification_result=verification_result,
                             notes=notes)
    db.commit()
    return redirect(f"/changes/{cid}")


@router.post("/changes/{cid}/comment")
def change_comment(cid: int, text: str = Form(""), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    changes.add_comment(db, user, get_or_404(db, ChangeRequest, cid), text)
    db.commit()
    return redirect(f"/changes/{cid}#timeline")
