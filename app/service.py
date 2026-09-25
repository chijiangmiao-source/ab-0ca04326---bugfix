"""规程补丁提交服务：校验、迟到补丁重放、幂等与拒绝规则。"""

from __future__ import annotations

import threading
from typing import Any, Dict, Tuple

from . import ot
from .store import Store


class Reject(Exception):
    """业务拒绝：HTTP 层映射为 409，且规程文本与修订号保持不变。"""

    def __init__(self, message: str, code: str = "rejected") -> None:
        super().__init__(message)
        self.code = code


class Service:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()

    def document(self) -> Dict[str, Any]:
        return self._store.snapshot()

    def create_draft(self) -> Dict[str, Any]:
        snap = self._store.snapshot()
        return {"base_revision": snap["revision"],
                "base_text": snap["text"],
                "revision": snap["revision"],
                "text": snap["text"]}

    # ------------------------------------------------------------------
    def _base_text(self, revision: int) -> str:
        snap = self._store.snapshot()
        if revision == snap["revision"]:
            return snap["text"]
        if revision == 0:
            return self._store.initial_text()
        rec = self._store.find_patch_by_revision(revision)
        if rec is None:
            raise Reject(f"修订号 {revision} 不存在", "unknown_revision")
        return rec["result_text"]

    @staticmethod
    def _canonical_delete_payload(payload: Dict[str, Any]):
        lo, hi, length = payload.get("lo"), payload.get("hi"), payload.get("length")
        if not isinstance(lo, int) or isinstance(lo, bool):
            return None
        if hi is None and length is None:
            return None
        if hi is not None and (not isinstance(hi, int) or isinstance(hi, bool)):
            return None
        if length is not None and (not isinstance(length, int)
                                   or isinstance(length, bool)):
            return None
        if hi is None:
            hi = lo + length
        if length is None:
            length = hi - lo
        if hi - lo != length:
            return None
        return {"lo": lo, "hi": hi, "length": length}

    def _same_payload(self, existing: Dict[str, Any], req: Dict[str, Any]) -> bool:
        incoming = req.get("payload")
        if not isinstance(incoming, dict):
            return False
        if existing["kind"] == "insert":
            return existing["payload"] == {"pos": incoming.get("pos"),
                                           "text": incoming.get("text")}
        canon = self._canonical_delete_payload(incoming)
        return canon is not None and canon == existing["payload"]

    @staticmethod
    def _parse_op(req: Dict[str, Any], base_len: int) -> Tuple[str, Tuple, Dict[str, Any]]:
        """校验并返回 (补丁标识, 内部操作, 规范化载荷)。

        删除的 hi / length 两种等价写法统一规范为 {lo, hi, length}，
        使 "相同载荷" 按语义而非字段写法判定。
        """
        pid = req.get("id")
        kind = req.get("kind")
        payload = req.get("payload")
        if not isinstance(pid, str) or not pid:
            raise Reject("补丁标识 id 必须是非空字符串", "bad_request")
        if not isinstance(payload, dict):
            raise Reject("payload 必须是对象", "bad_request")
        if kind == "insert":
            pos = payload.get("pos")
            text = payload.get("text")
            if not isinstance(pos, int) or isinstance(pos, bool):
                raise Reject("插入位置 pos 必须是整数", "bad_request")
            if not isinstance(text, str):
                raise Reject("插入内容 text 必须是字符串", "bad_request")
            if text == "":
                raise Reject("不允许空插入", "empty_insert")
            if pos < 0 or pos > base_len:
                raise Reject(f"插入位置 {pos} 越界（基准长度 {base_len}）",
                             "out_of_bounds")
            norm = {"pos": pos, "text": text}
            return pid, ot.make_ins(pos, text, pid), norm
        if kind == "delete":
            lo = payload.get("lo")
            hi = payload.get("hi")
            length = payload.get("length")
            if not isinstance(lo, int) or isinstance(lo, bool) or lo < 0:
                raise Reject("删除起点 lo 必须是非负整数", "bad_request")
            if hi is None and length is None:
                raise Reject("删除必须给出 hi 或 length", "bad_request")
            if hi is not None and (not isinstance(hi, int) or isinstance(hi, bool)):
                raise Reject("删除终点 hi 必须是整数", "bad_request")
            if length is not None and (not isinstance(length, int)
                                       or isinstance(length, bool)):
                raise Reject("删除长度 length 必须是整数", "bad_request")
            if hi is not None and length is not None and hi - lo != length:
                raise Reject(
                    f"长度不符：hi-lo={hi - lo} 与 length={length} 不一致",
                    "length_mismatch")
            if hi is None:
                hi = lo + length
            if length is None:
                length = hi - lo
            if length < 0:
                raise Reject("删除长度不能为负", "length_mismatch")
            if hi < lo:
                raise Reject("删除终点不能小于起点", "out_of_bounds")
            if hi > base_len:
                raise Reject(f"删除区间 [{lo},{hi}) 越界（基准长度 {base_len}）",
                             "out_of_bounds")
            norm = {"lo": lo, "hi": hi, "length": length}
            return pid, ot.make_del(lo, hi, pid), norm
        raise Reject("kind 必须是 insert 或 delete", "bad_request")

    @staticmethod
    def _validate_pieces(pieces, doc_len: int) -> None:
        for p in pieces:
            if p[0] == "ins":
                pos = p[1]
                if pos < 0 or pos > doc_len:
                    raise Reject(f"变换后插入位置 {pos} 越界", "out_of_bounds")
            else:
                lo, hi = p[1], p[2]
                if lo < 0 or hi < lo or hi > doc_len:
                    raise Reject(f"变换后删除区间 [{lo},{hi}) 越界",
                                 "out_of_bounds")

    def submit(self, req: Dict[str, Any]) -> Dict[str, Any]:
        base_revision = req.get("base_revision")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise Reject("base_revision 必须是整数", "bad_request")

        # 检查与提交整体串行，保证 "先查幂等/再确认" 的原子性
        with self._lock:
            snap = self._store.snapshot()
            current_revision = snap["revision"]

            # 幂等优先：同 id 已确认时，无论本次携带的 base_revision 为何，
            # 同载荷一律复现首次结果（不新增修订），不同载荷一律拒绝
            existing = self._store.find_patch(req.get("id", ""))
            if existing is not None:
                same = (existing["kind"] == req.get("kind")
                        and self._same_payload(existing, req))
                if not same:
                    # 标识复用但载荷不同：拒绝，规程保持不变
                    raise Reject(
                        f"补丁标识 {existing['id']!r} 已用于不同载荷，禁止复用",
                        "id_reused")
                # 同一标识携相同载荷重传：复现首次文本、修订与落点，
                # 不新增修订；同时附当前全文供页面刷新
                return {"id": existing["id"],
                        "revision": existing["revision"],
                        "base_revision": existing["base_revision"],
                        "kind": existing["kind"],
                        "payload": existing["payload"],
                        "pieces": existing["pieces"],
                        "text": existing["result_text"],
                        "current_revision": current_revision,
                        "current_text": snap["text"],
                        "idempotent": True}

            if base_revision > current_revision:
                raise Reject(
                    f"未来修订：base_revision={base_revision} "
                    f"大于当前修订 {current_revision}", "future_revision")

            base_text = self._base_text(base_revision)
            pid, op, norm_payload = self._parse_op(req, len(base_text))

            # 迟到补丁：依次变换越过基准修订之后的全部已确认补丁
            history = self._store.history_since(base_revision)
            pieces = ot.rebase(op, history)
            self._validate_pieces(pieces, len(snap["text"]))
            new_text = ot.apply_pieces(snap["text"], pieces)
            new_revision = current_revision + 1

            serial_pieces = [list(p) for p in pieces]
            record = {
                "id": pid,
                "base_revision": base_revision,
                "revision": new_revision,
                "kind": req["kind"],
                "payload": norm_payload,
                "pieces": serial_pieces,
                "base_text": base_text,
                "result_text": new_text,
            }
            self._store.commit(record, new_text)

            return {"id": pid,
                    "revision": new_revision,
                    "base_revision": base_revision,
                    "kind": req["kind"],
                    "payload": norm_payload,
                    "pieces": serial_pieces,
                    "text": new_text,
                    "current_revision": new_revision,
                    "idempotent": False}
