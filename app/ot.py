"""插入/删除补丁的操作变换（Operational Transform）。

内部操作表示（全部位置均为字符偏移，针对该操作所基于的文档状态）：

    ("ins", pos, text, patch_id)   在 pos 处插入 text
    ("del", lo,  hi,   patch_id)   删除半开区间 [lo, hi)

核心性质
--------
对同一基准状态上的两个并发操作 x、h，``transform(x, h)`` 返回一组
"改写后的 x"，使得先应用 h 再应用改写后的 x，与先应用 x 再应用
``transform(h, x)`` 得到完全相同的文本（TP1）。

确定序规则：

* 同位置插入按补丁标识字典序决定先后：标识小的留在原位，大的后移；
* 插入点落在并发删除区间内时，插入内容一律保留在删除形成的空隙处；
  从删除一侧看，删除区间被插入文本切开，文本同样保留；
* 删除与删除取原文字符集合的并集，重叠部分不重复删除。

因此任意一组并发补丁无论按什么先后顺序提交，最终文本都相同。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

Op = Tuple[str, int, int, str]  # 联合类型在 _make_* 中收窄
InsOp = Tuple[str, int, str, str]
DelOp = Tuple[str, int, int, str]
OpList = List[Tuple]


def make_ins(pos: int, text: str, patch_id: str,
             anchor: Optional[Tuple[int, ...]] = None) -> Tuple:
    """构造插入操作。

    anchor 为该插入首字符的全局定序身份（见 app.anchors）；带身份时
    同位置并发插入按身份（即原始空隙先后 + 标识字典序）定序，不再退
    化为只按标识排序，从而保证多个插入与删除交叠时仍与到达顺序无关。
    """
    return ("ins", int(pos), text, patch_id, anchor)


def make_del(lo: int, hi: int, patch_id: str) -> DelOp:
    return ("del", int(lo), int(hi), patch_id)


def _ins_anchor(op: Tuple):
    return op[4] if len(op) >= 5 else None


def _same_position_ins_first(x: Tuple, h: Tuple) -> bool:
    """两条同位置并发插入：x 是否应排在 h 之前。

    双方都带全局身份时按身份定序（身份已编码原始空隙与补丁标识）；
    否则退回补丁标识字典序规则。
    """
    ax, ah = _ins_anchor(x), _ins_anchor(h)
    if ax is not None and ah is not None:
        return ax < ah
    return x[3] < h[3]


def transform(x: Tuple, h: Tuple) -> OpList:
    """把并发操作 x 改写为 "h 已应用之后" 的等价操作（可能拆成多条）。"""
    if x[0] == "ins" and h[0] == "ins":
        _, p, text, pid = x[0], x[1], x[2], x[3]
        _, hp, htext = h[0], h[1], h[2]
        if p < hp:
            return [x]
        if p > hp:
            return [make_ins(p + len(htext), text, pid, _ins_anchor(x))]
        # 同位置插入：全局身份（原始空隙先后 + 标识字典序）小的在前
        if _same_position_ins_first(x, h):
            return [x]
        return [make_ins(p + len(htext), text, pid, _ins_anchor(x))]

    if x[0] == "ins" and h[0] == "del":
        _, p, text, pid = x[0], x[1], x[2], x[3]
        _, lo, hi, _ = h
        anchor = _ins_anchor(x)
        if p <= lo:
            return [x]                      # 插入在删除区间之前
        if p >= hi:
            return [make_ins(p - (hi - lo), text, pid, anchor)]  # 整体后移
        # 插入点落在被删除区间内：插入内容保留在删除空隙（等价于夹到 lo 处）；
        # 身份随操作保留，使多条被同一删除折叠的插入仍按原空隙先后排列
        return [make_ins(lo, text, pid, anchor)]

    if x[0] == "del" and h[0] == "ins":
        _, lo, hi, pid = x
        hp, htext = h[1], h[2]
        length = len(htext)
        if hp <= lo:
            return [make_del(lo + length, hi + length, pid)]  # 插入在删除前
        if hp >= hi:
            return [x]                                        # 插入在删除后
        # 插入文本位于删除区间内，必须保留：删除被切成左右两段
        return [make_del(lo, hp, pid), make_del(hp + length, hi + length, pid)]

    # del vs del：删除 x 在原文中尚未被 h 删除的部分（字符集合并）
    _, lo, hi, pid = x
    _, blo, bhi, _ = h
    blen = bhi - blo
    out: OpList = []
    if lo < blo:  # x 位于 h 之前的残段，坐标不变
        out.append(make_del(lo, min(hi, blo), pid))
    if bhi < hi:  # x 位于 h 之后的残段，整体前移 blen
        out.append(make_del(max(lo, bhi) - blen, hi - blen, pid))
    return out


def rebase(op: Tuple, history: Sequence[OpList]) -> OpList:
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
        pos, itext = pieces[0][1], pieces[0][2]
        return text[:pos] + itext + text[pos:]
    # 删除：各区间互斥，从后向前删除以免位移
    result = text
    for _, lo, hi, _ in sorted(pieces, key=lambda o: o[1], reverse=True):
        result = result[:lo] + result[hi:]
    return result
