"""插入/删除补丁的操作变换（Operational Transform）。

内部操作表示（位置均为字符偏移，针对该操作所基于的文档状态）：

    ("ins", pos, text, patch_id, origin)   在 pos 处插入 text
    ("del", lo,  hi,   patch_id, origin)   删除半开区间 [lo, hi)

第 5 项 ``origin`` 是该操作在 *其原始基准文档* 中的位置（插入为 pos、
删除为 lo），在一切变换中原样保留，不随当前坐标移动。旧版本落库的
四元组没有此项，读取时按当前位置回退（见 ``_origin``）。

核心性质
--------
对同一基准状态上的两个并发操作 x、h，``transform(x, h)`` 返回一组
"改写后的 x"，使得先应用 h 再应用改写后的 x，与先应用 x 再应用
``transform(h, x)`` 得到完全相同的文本（TP1）。

确定序规则：

* 当前位置相同的插入（典型情形：并发删除把不同原始位置的插入挤压到
  同一删除空隙）按 ``(origin, 补丁标识)`` 字典序决定先后：原始位置
  小的留在原位，大的后移；origin 也相同时再按补丁标识定序；
* 插入点落在并发删除区间内时，插入内容一律保留在删除形成的空隙处；
  从删除一侧看，删除区间被插入文本切开，文本同样保留；
* 删除与删除取原文字符集合的并集，重叠部分不重复删除。

因此任意一组并发补丁（含插入与删除交叠）无论按什么先后顺序提交，
最终文本都相同。
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

InsOp = Tuple[str, int, str, str, int]
DelOp = Tuple[str, int, int, str, int]
Op = Tuple[Any, ...]  # 联合类型在 make_* 中收窄
OpList = List[Op]


def make_ins(pos: int, text: str, patch_id: str) -> InsOp:
    pos = int(pos)
    return ("ins", pos, text, patch_id, pos)


def make_del(lo: int, hi: int, patch_id: str) -> DelOp:
    lo, hi = int(lo), int(hi)
    return ("del", lo, hi, patch_id, lo)


def _origin(op: Op) -> int:
    """操作在其原始基准文档中的位置；旧版四元组记录按当前位置回退。"""
    return op[4] if len(op) >= 5 else op[1]


def transform(x: Op, h: Op) -> OpList:
    """把并发操作 x 改写为 "h 已应用之后" 的等价操作（可能拆成多条）。"""
    if x[0] == "ins" and h[0] == "ins":
        _, p, text, pid = x[:4]
        _, hp, htext, hid = h[:4]
        if p < hp:
            return [x]
        if p > hp:
            return [("ins", p + len(htext), text, pid, _origin(x))]
        # 当前位置相同（常由并发删除把不同原始位置的插入挤到同一空隙）：
        # 先按原始基准位置、再按补丁标识定序，保证与到达顺序无关
        if (_origin(x), pid) < (_origin(h), hid):
            return [x]
        return [("ins", p + len(htext), text, pid, _origin(x))]

    if x[0] == "ins" and h[0] == "del":
        _, p, text, pid = x[:4]
        _, lo, hi, _ = h[:4]
        if p <= lo:
            return [x]                      # 插入在删除区间之前
        if p >= hi:
            return [("ins", p - (hi - lo), text, pid, _origin(x))]  # 整体后移
        # 插入点落在被删除区间内：插入内容保留在删除空隙（等价于夹到 lo 处）
        return [("ins", lo, text, pid, _origin(x))]

    if x[0] == "del" and h[0] == "ins":
        _, lo, hi, pid = x[:4]
        _, hp, htext, _ = h[:4]
        length = len(htext)
        if hp <= lo:
            # 插入在删除前
            return [("del", lo + length, hi + length, pid, _origin(x))]
        if hp >= hi:
            return [x]                                        # 插入在删除后
        # 插入文本位于删除区间内，必须保留：删除被切成左右两段
        return [("del", lo, hp, pid, _origin(x)),
                ("del", hp + length, hi + length, pid, _origin(x))]

    # del vs del：删除 x 在原文中尚未被 h 删除的部分（字符集合并）
    _, lo, hi, pid = x[:4]
    _, blo, bhi, _ = h[:4]
    blen = bhi - blo
    out: OpList = []
    if lo < blo:  # x 位于 h 之前的残段，坐标不变
        out.append(("del", lo, min(hi, blo), pid, _origin(x)))
    if bhi < hi:  # x 位于 h 之后的残段，整体前移 blen
        out.append(("del", max(lo, bhi) - blen, hi - blen, pid, _origin(x)))
    return out


def rebase(op: Op, history: Sequence[OpList]) -> OpList:
    """把基于旧修订的 op 依次改写越过 history 中各已确认修订。

    history 为每个已确认修订保存其 "实际落库形态"（rebase 后可能是
    多条互斥操作）。返回 op 在当前最新文档上的等价操作列表。
    """
    pieces: OpList = [op]
    for applied in history:
        # 同一修订落库的多个互斥片段共享同一输入态坐标，必须按服务端
        # 实际应用顺序越过：删除段从右向左（apply_pieces 的顺序），
        # 这样越过靠后的段后靠前的段坐标仍然有效；插入修订只有一条。
        if all(p[0] == "del" for p in applied):
            ordered = sorted(applied, key=lambda o: o[1], reverse=True)
        else:
            ordered = applied
        for h in ordered:
            rebased: OpList = []
            for piece in pieces:
                rebased.extend(transform(piece, h))
            pieces = rebased
    return pieces


def apply_pieces(text: str, pieces: OpList) -> str:
    """把 rebase 后的一组操作落到文本上。"""
    if not pieces:
        return text
    if pieces[0][0] == "ins":
        # 一次补丁经过变换后至多仍是一条插入
        _, pos, itext, _, _ = pieces[0]
        return text[:pos] + itext + text[pos:]
    # 删除：各区间互斥，从后向前删除以免位移
    result = text
    for _, lo, hi, _, _ in sorted(pieces, key=lambda o: o[1], reverse=True):
        result = result[:lo] + result[hi:]
    return result
