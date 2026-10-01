from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import assets, changes
from app.errors import DomainError, PermissionDenied
from app.models import ChangeResult, ChangeStatus, Event, SkipPolicy, StepStatus


def _execs(cr):
    return {(s.code, e.target.hostname): e for s in cr.steps for e in s.executions}


def test_full_lifecycle(db, f, world):
    w = world
    tpl = f.template(w.editor, "A", [(w.precheck, SkipPolicy.REQUIRED), (w.vlan, SkipPolicy.REQUIRED),
                                     (w.nic, SkipPolicy.CONDITIONAL)])
    cr = f.change(w.req, template=tpl, devices=[w.sw, w.web])

    # 模組依設備類型自動掛勾：A01 兩台都要做，A03 只掛交換器，L01 只掛 Linux。
    ex = _execs(cr)
    assert set(ex) == {("A01", "core-sw-01"), ("A01", "web-prod-01"), ("A03", "core-sw-01"), ("L01", "web-prod-01")}
    assert cr.number.startswith("CR-")

    changes.transition(db, w.req, cr, "submit")
    with pytest.raises(PermissionDenied):
        changes.transition(db, w.req, cr, "approve")  # 職責分離
    changes.transition(db, w.appr, cr, "approve")
    changes.transition(db, w.op_net, cr, "start")
    assert cr.status == ChangeStatus.IN_PROGRESS and cr.started_at

    with pytest.raises(PermissionDenied):
        changes.update_execution(db, w.op_net, ex[("L01", "web-prod-01")], status=StepStatus.DONE)
    with pytest.raises(DomainError, match="必要步驟不可省略"):
        changes.update_execution(db, w.op_linux, ex[("A01", "web-prod-01")], status=StepStatus.SKIPPED, note="x")
    with pytest.raises(DomainError, match="必須填寫原因"):
        changes.update_execution(db, w.op_linux, ex[("L01", "web-prod-01")], status=StepStatus.SKIPPED)
    changes.update_execution(db, w.op_linux, ex[("L01", "web-prod-01")], status=StepStatus.SKIPPED, note="已有網卡")

    with pytest.raises(DomainError, match="尚有 3 個步驟未處理"):
        changes.transition(db, w.op_net, cr, "finish")
    changes.update_execution(db, w.op_net, ex[("A01", "core-sw-01")], status=StepStatus.DONE)
    changes.update_execution(db, w.op_net, ex[("A03", "core-sw-01")], status=StepStatus.DONE, output="ok")
    changes.update_execution(db, w.op_linux, ex[("A01", "web-prod-01")], status=StepStatus.DONE)
    changes.transition(db, w.op_net, cr, "finish")
    assert cr.status == ChangeStatus.EXECUTED

    with pytest.raises(PermissionDenied):
        changes.record_and_close(db, w.op_net, cr, result=ChangeResult.SUCCESS, verification_result="ok")
    with pytest.raises(DomainError, match="驗證結果"):
        changes.record_and_close(db, w.req, cr, result=ChangeResult.SUCCESS, verification_result=" ")
    changes.record_and_close(db, w.req, cr, result=ChangeResult.SUCCESS, verification_result="ping OK")
    assert cr.status == ChangeStatus.CLOSED

    db.flush()
    actions = db.scalars(select(Event.action).filter_by(change_id=cr.id).order_by(Event.id)).all()
    assert actions[0] == "change.created"
    assert [a for a in actions if a.startswith("change.")][1:] == [
        "change.submit", "change.approve", "change.start", "change.finish", "change.closed"]
    assert actions.count("step.executed") == 4


def test_submit_requires_every_step_and_device_hooked(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.web], modules=[w.vlan])  # A03 只適用網路設備，掛不到 Linux
    with pytest.raises(DomainError, match="每個步驟都必須掛到至少一台設備"):
        changes.transition(db, w.req, cr, "submit")
    changes.toggle_applicability(db, w.req, cr.steps[0], cr.targets[0].id)
    changes.transition(db, w.req, cr, "submit")
    assert cr.status == ChangeStatus.SUBMITTED


def test_plan_locked_after_submit(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck])
    changes.transition(db, w.req, cr, "submit")
    with pytest.raises(DomainError, match="草稿"):
        changes.add_target(db, w.req, cr, w.web.id)


def test_start_only_within_window(db, f, world):
    w = world
    t = changes.now()
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck], window_start=t + timedelta(hours=1),
                  window_end=t + timedelta(hours=2))
    changes.transition(db, w.req, cr, "submit")
    changes.transition(db, w.appr, cr, "approve")
    with pytest.raises(DomainError, match="時段"):
        changes.transition(db, w.op_net, cr, "start")


def test_scoped_approver_must_cover_all_targets(db, f, world):
    w = world
    net_appr = f.user("net_appr", ("APPROVER", "NETWORK", None))
    cr = f.change(w.req, devices=[w.sw, w.web], modules=[w.precheck])
    changes.transition(db, w.req, cr, "submit")
    with pytest.raises(PermissionDenied, match="未涵蓋"):
        changes.transition(db, net_appr, cr, "approve")


def test_reject_and_cancel_require_reason(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck])
    changes.transition(db, w.req, cr, "submit")
    with pytest.raises(DomainError, match="原因"):
        changes.transition(db, w.appr, cr, "reject")
    changes.transition(db, w.appr, cr, "reject", comment="時段衝突")
    assert cr.status == ChangeStatus.DRAFT
    with pytest.raises(DomainError, match="原因"):
        changes.transition(db, w.req, cr, "cancel")
    changes.transition(db, w.req, cr, "cancel", comment="不需要了")
    assert cr.status == ChangeStatus.CANCELLED


def test_chat_source_limited_to_basic_actions(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck])
    with pytest.raises(DomainError, match="網頁"):
        changes.transition(db, w.req, cr, "submit", source="chat")


def test_device_snapshot_survives_device_change(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck])
    w.sw.ip = "10.99.0.1"
    db.flush()
    assert str(cr.targets[0].ip) == "10.10.1.1"


def test_events_are_append_only(db, f, world):
    f.change(world.req)
    db.flush()
    for sql in ("UPDATE events SET action = 'x'", "DELETE FROM events",
                "UPDATE step_module_revisions SET command = 'x'"):
        with pytest.raises(DBAPIError, match="append-only"):
            with db.begin_nested():
                db.execute(text(sql))


def test_device_update_is_logged_and_validated(db, f, world):
    admin = f.user("admin", "ADMIN")
    with pytest.raises(PermissionDenied):
        assets.update_device(db, world.req, world.sw, is_active=True, hostname="x", device_type="NETWORK")
    with pytest.raises(DomainError, match="IP"):
        assets.update_device(db, admin, world.sw, is_active=True, hostname="core-sw-01", device_type="NETWORK",
                             ip="10.0.0.0/8")
    assets.update_device(db, admin, world.sw, is_active=True, hostname="core-sw-99", device_type="NETWORK",
                         ip="10.10.1.2")
    db.flush()
    ev = db.scalars(select(Event).filter_by(action="device.updated")).one()
    assert ev.detail["hostname"] == "core-sw-01 → core-sw-99" and ev.detail["ip"] == "10.10.1.1 → 10.10.1.2"
