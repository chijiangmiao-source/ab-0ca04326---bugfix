"""插入字符的全局定序锚点（与补丁到达顺序无关）。

问题背景
--------
纯位置坐标的 OT 在把 "落在并发删除区间内的插入" 折叠到删除边界位置
时，会丢掉两条插入原本的相对先后：它们都变成同一位置，只能退回按
补丁标识排序，于是三个以上并发补丁（两插入 + 一删除）随提交排列
不同而不收敛（TP2 缺失）。

解决办法
--------
给每个字符一个全局可比较的身份元组，字典序即文档顺序：

* 修订号 0 全文的第 ``i`` 个字符身份为 ``(2*i+1,)``；
* 在某个修订状态的可见字符边界（左身份 L、右身份 R）插入时，新字符
  身份严格落在 ``L`` 与 ``R`` 之间，并以补丁标识编码消歧：

      between(L, R) + 编码(patch_id) + (字符序号,)

  同位置并发插入因此天然按补丁标识字典序排列（与既有规则一致）；
  原本位置不同、只是被删除空隙折叠到一起的插入则保持原边界先后；
* 删除只令其基准视图中 ``[lo, hi)`` 范围内可见字符的身份失效
  （墓碑）。并发插入不在该视图内，身份永不失效——对应"插入跨越
  删除一律保留"；基准视图内的既有插入则可以被正常删除。

于是任意一组基于同一旧修订的补丁，其插入字符在每个空隙的相对先后
都是补丁内容的确定函数，与网络到达顺序无关。

``AnchorIndex`` 按修订号保存（已插入字符、已失效字符身份）的快照；
服务每次确认补丁时从其基准修订的视图定位身份，再推进到当前修订，
重启后按补丁历史顺序重放即可完全重建。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

Id = Tuple[int, ...]


def _between(left: Optional[Id], right: Optional[Id]) -> Id:
    """返回严格满足 left < t < right 的整数元组（None 表示 ±∞）。"""
    li = left or ()
    ri = right or ()
    prefix: List[int] = []
    i = 0
    while True:
        l = li[i] if i < len(li) else None
        r = ri[i] if i < len(ri) else None
        if l is not None and r is not None and r - l >= 2:
            return tuple(prefix + [l + (r - l) // 2])
        if l is not None and r is None:
            return tuple(prefix + [l + 1])
        if l is None and r is not None:
            return tuple(prefix + [r - 1])
        if l is None and r is None:
            return tuple(prefix + [0])
        # 该层没有整数空隙：沿公共值向更深一层继续
        prefix.append(l)  # type: ignore[arg-type]
        i += 1


def _encode_pid(pid: str) -> Tuple[int, ...]:
    # 全部 ≥1：字符串前缀关系映射为元组前缀关系，且与字符序号 0 不冲突，
    # 因而同边界插入的首字符身份顺序恰好等于补丁标识字典序。
    return tuple(ord(ch) + 1 for ch in pid)


class AnchorIndex:
    def __init__(self, initial_text: str) -> None:
        self._base: List[str] = list(initial_text)
        self._insert_char: Dict[Id, str] = {}
        self._anchor_of: Dict[str, Tuple[Id, ...]] = {}
        self._rev = 0
        self._ins: Dict[int, frozenset[Id]] = {0: frozenset()}
        self._dead: Dict[int, frozenset[Id]] = {0: frozenset()}

    @property
    def revision(self) -> int:
        return self._rev

    # ---- 视图 ----------------------------------------------------------
    def _view(self, revision: int) -> List[Tuple[Id, str]]:
        """某修订号下的可见字符序列（身份、字符），按身份字典序排列。"""
        dead = self._dead[revision]
        entries: List[Tuple[Id, str]] = [
            ((2 * i + 1,), ch)
            for i, ch in enumerate(self._base) if (2 * i + 1,) not in dead
        ]
        entries.extend((cid, self._insert_char[cid])
                       for cid in self._ins[revision] if cid not in dead)
        entries.sort(key=lambda e: e[0])
        return entries

    def render(self, revision: Optional[int] = None) -> str:
        return "".join(ch for _, ch in
                       self._view(self._rev if revision is None else revision))

    def _render(self, ins: frozenset[Id], dead: frozenset[Id],
                chars: Optional[Dict[Id, str]] = None) -> str:
        chars = chars if chars is not None else self._insert_char
        entries: List[Tuple[Id, str]] = [
            ((2 * i + 1,), ch)
            for i, ch in enumerate(self._base) if (2 * i + 1,) not in dead
        ]
        entries.extend((cid, chars[cid])
                       for cid in ins if cid not in dead)
        entries.sort(key=lambda e: e[0])
        return "".join(ch for _, ch in entries)

    def preview_insert(self, ids: Tuple[Id, ...], text: str) -> str:
        chars = dict(self._insert_char)
        chars.update(zip(ids, text))
        return self._render(self._ins[self._rev] | frozenset(ids),
                            self._dead[self._rev], chars)

    def preview_delete(self, killed: frozenset[Id]) -> str:
        return self._render(self._ins[self._rev],
                            self._dead[self._rev] | killed)

    def anchor_of(self, patch_id: str) -> Tuple[Id, ...]:
        return self._anchor_of[patch_id]

    # ---- 两种补丁效果的身份计算 ----------------------------------------
    def insert_ids(self, base_revision: int, pos: int, text: str,
                   patch_id: str) -> Tuple[Id, ...]:
        view = self._view(base_revision)
        left = view[pos - 1][0] if pos > 0 else None
        right = view[pos][0] if pos < len(view) else None
        core = _between(left, right) + _encode_pid(patch_id)
        return tuple(core + (k,) for k in range(len(text)))

    def deleted_ids(self, base_revision: int, lo: int, hi: int) -> frozenset[Id]:
        """基准视图 [lo,hi) 内全部可见字符的身份（含其中的既有插入）。"""
        return frozenset(cid for cid, _ in self._view(base_revision)[lo:hi])

    # ---- 推进修订 ------------------------------------------------------
    def commit_insert(self, patch_id: str, ids: Tuple[Id, ...],
                      text: str) -> None:
        nxt = self._rev + 1
        for cid, ch in zip(ids, text):
            self._insert_char[cid] = ch
        self._anchor_of[patch_id] = tuple(ids)
        self._ins[nxt] = self._ins[self._rev] | frozenset(ids)
        self._dead[nxt] = self._dead[self._rev]
        self._rev = nxt

    def commit_delete(self, killed: frozenset[Id]) -> None:
        nxt = self._rev + 1
        self._ins[nxt] = self._ins[self._rev]
        self._dead[nxt] = self._dead[self._rev] | frozenset(killed)
        self._rev = nxt

    # ---- 历史重放（重启后重建）-----------------------------------------
    def replay_record(self, rec: Dict[str, Any]) -> None:
        payload = rec["payload"]
        if rec["kind"] == "insert":
            ids = self.insert_ids(rec["base_revision"], payload["pos"],
                                  payload["text"], rec["id"])
            self.commit_insert(rec["id"], ids, payload["text"])
        else:
            killed = self.deleted_ids(rec["base_revision"],
                                      payload["lo"], payload["hi"])
            self.commit_delete(killed)
