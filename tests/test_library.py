import pytest

from app import changes, library
from app.errors import DomainError, PermissionDenied
from app.models import ChangeResult, ModuleStatus, ReviewDecision, SkipPolicy, StepStatus


def run_to_close(db, w, cr):
    changes.transition(db, w.req, cr, "submit")
    changes.transition(db, w.appr, cr, "approve")
    changes.transition(db, w.op_net, cr, "start")
    for s in cr.steps:
        for e in s.executions:
            changes.update_execution(db, w.op_net, e, status=StepStatus.DONE)
    changes.transition(db, w.op_net, cr, "finish")
    changes.record_and_close(db, w.req, cr, result=ChangeResult.SUCCESS, verification_result="ok")


def edit(db, w, step, command, note=""):
    changes.update_step(db, w.req, step, seq=step.seq, skip_policy=step.skip_policy, modification_note=note,
                        instruction=step.instruction, command=command, verification=step.verification,
                        rollback=step.rollback)


def test_modification_detection(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.vlan])
    step = cr.steps[0]
    with pytest.raises(DomainError, match="調整原因"):
        edit(db, w, step, "vlan 300")
    edit(db, w, step, "vlan 300\r\n name web", note="此次使用 VLAN 300")
    assert step.is_modified and step.command == "vlan 300\n name web"
    edit(db, w, step, "vlan 200\r\n")  # 改回原內容（含瀏覽器換行）即不算偏離
    assert not step.is_modified


def test_promote_variant_and_merge_parent(db, f, world):
    w = world
    cr1 = f.change(w.req, devices=[w.sw], modules=[w.vlan])
    edit(db, w, cr1.steps[0], "vlan 200\nspanning-tree vlan 200", note="需調整 STP")
    assert library.pending_reviews(db) == []  # 結案前不進審核
    run_to_close(db, w, cr1)
    assert library.pending_reviews(db) == [cr1.steps[0]]

    with pytest.raises(PermissionDenied):
        library.review_step(db, w.req, cr1.steps[0], ReviewDecision.PROMOTE)
    library.review_step(db, w.editor, cr1.steps[0], ReviewDecision.PROMOTE)
    variant = cr1.steps[0].result_module
    assert (variant.code, variant.parent_id, variant.status) == ("A03-2", w.vlan.id, ModuleStatus.STANDARD)
    assert variant.current.command == "vlan 200\nspanning-tree vlan 200"
    assert library.pending_reviews(db) == []

    cr2 = f.change(w.req, devices=[w.sw], modules=[w.vlan])
    edit(db, w, cr2.steps[0], "vlan 200\nname web", note="補上名稱")
    run_to_close(db, w, cr2)
    library.review_step(db, w.editor, cr2.steps[0], ReviewDecision.MERGE_PARENT)
    assert w.vlan.current_revision == 2 and w.vlan.current.command == "vlan 200\nname web"
    # 歷史變更仍指向當時使用的版本
    assert cr1.steps[0].module_revision == 1


def test_merge_blocked_when_parent_moved_on(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.vlan])
    edit(db, w, cr.steps[0], "vlan 201", note="x")
    run_to_close(db, w, cr)
    library.revise_module(db, w.editor, w.vlan, note="修正", instruction="新說明", command="vlan 200",
                          verification="verify", rollback="rollback")
    with pytest.raises(DomainError, match="新版本"):
        library.review_step(db, w.editor, cr.steps[0], ReviewDecision.MERGE_PARENT)


def test_custom_step_promoted_as_new_module(db, f, world):
    w = world
    cr = f.change(w.req, devices=[w.sw], modules=[w.precheck])
    changes.add_custom_step(db, w.req, cr, title="清除 ARP", skip_policy=SkipPolicy.OPTIONAL,
                            modification_note="新流程", command="clear arp")
    run_to_close(db, w, cr)
    custom = cr.steps[1]
    with pytest.raises(DomainError, match="代碼"):
        library.review_step(db, w.editor, custom, ReviewDecision.PROMOTE)
    library.review_step(db, w.editor, custom, ReviewDecision.PROMOTE, code="A20", device_type="NETWORK")
    assert custom.result_module.code == "A20" and custom.result_module.parent_id is None


def test_module_lifecycle_and_permissions(db, f, world):
    w = world
    plain_editor = f.user("plain_editor", "MODULE_EDITOR")
    m = library.create_module(db, plain_editor, code="W01", title="重啟 IIS", device_type="WINDOWS",
                              command="iisreset")
    assert m.status == ModuleStatus.CANDIDATE
    cr = f.change(w.req)
    with pytest.raises(DomainError, match="標準模組"):
        changes.add_module_step(db, w.req, cr, m.id, SkipPolicy.REQUIRED)
    with pytest.raises(PermissionDenied):
        library.set_module_status(db, plain_editor, m, ModuleStatus.STANDARD)
    library.set_module_status(db, w.editor, m, ModuleStatus.STANDARD)
    with pytest.raises(PermissionDenied):
        library.revise_module(db, plain_editor, m, note="x", command="iisreset /noforce")
    with pytest.raises(DomainError, match="沒有變更"):
        library.revise_module(db, w.editor, m, note="x", command="iisreset")
    assert library.next_variant_code(db, "W01") == "W01-2"
