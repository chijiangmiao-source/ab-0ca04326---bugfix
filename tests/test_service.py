"""Service/Store 业务规则测试（零第三方依赖，同时兼容 pytest）。"""

from __future__ import annotations

import json
import os
import tempfile

from app.service import Reject, Service
from app.store import Store

BASE = "0123456789"  # 10 字符


def new_service():
    tmp = tempfile.mkdtemp(prefix="ot-test-")
    return Service(Store(os.path.join(tmp, "state.json"), initial_text=BASE))


def expect_reject(fn, code):
    try:
        fn()
    except Reject as exc:
        assert exc.code == code, f"期望拒绝码 {code}，实际 {exc.code}"
        return
    raise AssertionError(f"期望被拒绝（{code}），但提交成功")


def ins(pid, pos, text, rev=0):
    return {"id": pid, "kind": "insert", "base_revision": rev,
            "payload": {"pos": pos, "text": text}}


def dele(pid, lo, hi=None, length=None, rev=0):
    payload = {"lo": lo}
    if hi is not None:
        payload["hi"] = hi
    if length is not None:
        payload["length"] = length
    return {"id": pid, "kind": "delete", "base_revision": rev, "payload": payload}


# ---- 草案与读取 --------------------------------------------------------
def test_draft_returns_full_text_and_revision():
    svc = new_service()
    d = svc.create_draft()
    assert d["base_revision"] == 0
    assert d["base_text"] == BASE
    assert svc.document() == {"text": BASE, "revision": 0}


# ---- 基本确认：全文、连续修订、实际落点 -------------------------------
def test_insert_confirmation():
    svc = new_service()
    r = svc.submit(ins("p1", 3, "XY"))
    assert r["revision"] == 1
    assert r["text"] == "012XY3456789"
    assert r["pieces"] == [["ins", 3, "XY", "p1"]]
    assert svc.document()["revision"] == 1


def test_consecutive_revisions():
    svc = new_service()
    r1 = svc.submit(ins("p1", 0, "A"))
    r2 = svc.submit(ins("p2", 0, "B", rev=1))
    assert (r1["revision"], r2["revision"]) == (1, 2)
    assert r2["text"] == "BA" + BASE


# ---- 幂等：同 id 同载荷重传复现首次结果，不新增修订 --------------------
def test_idempotent_retransmit():
    svc = new_service()
    r1 = svc.submit(ins("dup", 2, "K"))
    r2 = svc.submit(ins("dup", 2, "K"))
    assert r1["revision"] == r2["revision"] == 1
    assert r2["text"] == r1["text"]
    assert r2["pieces"] == r1["pieces"]
    assert r2["idempotent"] is True
    assert svc.document()["revision"] == 1


def test_idempotent_retransmit_after_other_patches():
    svc = new_service()
    svc.submit(ins("a", 0, "A"))
    first = svc.submit(ins("late", 0, "L"))
    assert first["revision"] == 2
    svc.submit(ins("b", 0, "B", rev=2))
    again = svc.submit(ins("late", 0, "L"))  # base_revision 仍为 0
    assert again["idempotent"] is True
    assert again["revision"] == 2
    assert again["text"] == first["text"]
    assert again["pieces"] == first["pieces"]
    assert svc.document()["revision"] == 3  # 没有新增修订


# ---- 拒绝规则 ----------------------------------------------------------
def test_idempotent_delete_equivalent_forms():
    # 首次用 hi，重传只用 length（语义相同）：仍幂等，不新增修订
    svc = new_service()
    r1 = svc.submit(dele("d", 2, hi=5))
    r2 = svc.submit(dele("d", 2, length=3))
    assert r2["idempotent"] is True
    assert r2["revision"] == 1
    assert r2["text"] == r1["text"]
    assert svc.document()["revision"] == 1
    # 语义不同（长度不同）则按标识复用拒绝
    expect_reject(lambda: svc.submit(dele("d", 2, length=4)), "id_reused")


def test_reject_id_reuse_with_different_payload():
    svc = new_service()
    svc.submit(ins("same-id", 1, "X"))
    before = svc.document()
    expect_reject(lambda: svc.submit(ins("same-id", 2, "Y")), "id_reused")
    assert svc.document() == before


def test_reject_id_reuse_different_kind():
    svc = new_service()
    svc.submit(ins("same-id", 1, "X"))
    before = svc.document()
    expect_reject(lambda: svc.submit(dele("same-id", 1, 2)), "id_reused")
    assert svc.document() == before


def test_reject_future_revision():
    svc = new_service()
    expect_reject(lambda: svc.submit(ins("f", 0, "x", rev=5)),
                  "future_revision")
    assert svc.document()["revision"] == 0


def test_reject_insert_out_of_bounds():
    svc = new_service()
    expect_reject(lambda: svc.submit(ins("o", 11, "x")), "out_of_bounds")
    expect_reject(lambda: svc.submit(ins("o", -1, "x")), "out_of_bounds")
    assert svc.document()["text"] == BASE


def test_reject_delete_out_of_bounds():
    svc = new_service()
    expect_reject(lambda: svc.submit(dele("o", 8, 11)), "out_of_bounds")
    assert svc.document()["text"] == BASE


def test_reject_delete_length_mismatch():
    svc = new_service()
    expect_reject(lambda: svc.submit(dele("m", 2, hi=5, length=4)),
                  "length_mismatch")
    assert svc.document() == {"text": BASE, "revision": 0}


def test_accept_delete_with_matching_length():
    svc = new_service()
    r = svc.submit(dele("m", 2, hi=5, length=3))
    assert r["text"] == "0156789"


def test_reject_empty_insert():
    svc = new_service()
    expect_reject(lambda: svc.submit(ins("e", 0, "")), "empty_insert")


# ---- 迟到补丁：越过已确认补丁 ------------------------------------------
def test_late_insert_transformed_over_delete():
    svc = new_service()
    svc.submit(dele("d", 3, 6))  # 已确认删除 "345"
    # 迟到插入基于修订 0，位置 4（落在删除区间内）→ 内容保留在空隙
    r = svc.submit(ins("late", 4, "K"))
    assert r["base_revision"] == 0
    assert r["revision"] == 2
    assert r["text"] == "012K6789"
    assert r["pieces"] == [["ins", 3, "K", "late"]]


def test_late_insert_after_delete_shifts_back():
    svc = new_service()
    svc.submit(dele("d", 2, 4))  # 删 "23"
    r = svc.submit(ins("late", 8, "Z"))  # 原 pos 8 在删除之后 → 前移 2
    assert r["pieces"] == [["ins", 6, "Z", "late"]]
    assert r["text"] == "014567Z89"


def test_late_delete_split_by_insert():
    svc = new_service()
    svc.submit(ins("i", 5, "K"))  # 修订 1: "01234K56789"
    r = svc.submit(dele("late", 4, 7))
    assert r["pieces"] == [["del", 4, 5, "late"],
                           ["del", 6, 8, "late"]]
    assert r["text"] == "0123K789"


def test_late_overlapping_delete_takes_union():
    svc = new_service()
    svc.submit(dele("d1", 2, 7))
    r = svc.submit(dele("d2", 5, 9))  # 并集 [2,9)
    assert r["text"] == "019"


# ---- 两份并发补丁无论提交先后均收敛（服务层等价性） --------------------
def _run_two(order, tmp):
    svc = Service(Store(os.path.join(tmp, f"s-{order}.json"),
                        initial_text=BASE))
    a = ins("p-a", 4, "AA")
    b = ins("p-b", 4, "BB")
    if order == "ab":
        svc.submit(a)
        svc.submit(b)
    else:
        svc.submit(b)
        svc.submit(a)
    return svc.document()["text"], svc.document()["revision"]


def test_concurrent_same_position_inserts_service_convergence():
    tmp = tempfile.mkdtemp(prefix="ot-conv-")
    t1, r1 = _run_two("ab", tmp)
    t2, r2 = _run_two("ba", tmp)
    assert t1 == t2 == "0123AABB456789"
    assert r1 == r2 == 2


def test_concurrent_insert_inside_delete_service_convergence():
    def run(order):
        tmp = tempfile.mkdtemp(prefix="ot-conv-")
        svc = Service(Store(os.path.join(tmp, f"x-{order}.json"),
                            initial_text=BASE))
        d = dele("del", 2, 7)
        i = ins("ins", 4, "KEEP")
        if order == "ab":
            svc.submit(d)
            svc.submit(i)
        else:
            svc.submit(i)
            svc.submit(d)
        return svc.document()["text"]
    assert run("ab") == run("ba") == "01KEEP789"


def test_rejected_submit_leaves_state_untouched():
    svc = new_service()
    svc.submit(dele("d", 0, 3))
    before = svc.document()
    expect_reject(lambda: svc.submit(ins("bad", 999, "X")), "out_of_bounds")
    assert svc.document() == before


# ---- 原子持久化与重启 --------------------------------------------------
def test_restart_replays_old_revision_patches():
    tmp = tempfile.mkdtemp(prefix="ot-restart-")
    path = os.path.join(tmp, "state.json")
    s1 = Service(Store(path, initial_text=BASE))
    s1.submit(ins("i", 5, "K"))  # 修订 1
    # 重启后仍可基于旧修订 0 提交合法补丁
    s2 = Service(Store(path))
    r = s2.submit(dele("late", 4, 7))  # 越过 rev1 的插入
    assert r["text"] == "0123K789"
    assert s2.document()["revision"] == 2
    # 再次重启，状态无变化
    s3 = Service(Store(path))
    assert s3.document() == {"text": "0123K789", "revision": 2}
    # 旧补丁幂等重传在重启后仍复现首次结果
    again = s3.submit(ins("i", 5, "K"))
    assert again["idempotent"] is True
    assert again["text"] == "01234K56789"
    assert s3.document()["revision"] == 2


def test_state_file_single_json_and_no_temp_leftovers():
    tmp = tempfile.mkdtemp(prefix="ot-file-")
    path = os.path.join(tmp, "state.json")
    svc = Service(Store(path, initial_text=BASE))
    svc.submit(ins("p", 1, "Q"))
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["revision"] == 1
    assert data["text"] == "0Q123456789"
    assert data["patches"][0]["pieces"] == [["ins", 1, "Q", "p"]]
    leftovers = [n for n in os.listdir(tmp) if n.startswith(".state-")]
    assert leftovers == []
