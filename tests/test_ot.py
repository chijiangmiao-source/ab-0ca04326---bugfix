"""OT 变换收敛性测试。

系统采用中央服务器单一全序（Jupiter 星型）：每个迟到补丁只沿服务端
提交顺序依次重放。需要保证的核心性质是 TP1 —— 同一基准上的 *两份*
并发补丁，无论谁先到服务端，最终文本相同；纯插入场景因同点按补丁
标识字典序定序，任意数量都收敛。多补丁场景在固定服务端顺序下结果
确定（服务端怎么算，终端就看到什么）。
"""

from __future__ import annotations

import itertools
import random

from app import ot


def _ordered(pieces):
    """与 service 落库顺序一致：删除段从右向左，插入只有一条。"""
    if all(p[0] == "del" for p in pieces):
        return sorted(pieces, key=lambda o: o[1], reverse=True)
    return pieces


def simulate_order(base, ops, order):
    text = base
    history = []
    revisions = {}
    for idx in order:
        pieces = ot.rebase(ops[idx], history)
        text = ot.apply_pieces(text, pieces)
        history.append(_ordered(pieces))
        revisions[idx] = (len(history), pieces, text)
    return text, revisions


def assert_pair_convergent(base, a, b):
    t1, _ = simulate_order(base, [a, b], (0, 1))
    t2, _ = simulate_order(base, [a, b], (1, 0))
    assert t1 == t2, (
        f"两份并发补丁未收敛：{a} vs {b}\n  a 先提交: {t1!r}\n  b 先提交: {t2!r}")
    return t1


def assert_all_orders_convergent(base, ops):
    """纯插入等满足全序收敛的场景：所有提交排列结果一致。"""
    texts = set()
    for order in itertools.permutations(range(len(ops))):
        text, _ = simulate_order(base, ops, order)
        texts.add(text)
    assert len(texts) == 1, f"未收敛：{texts!r}"
    return texts.pop()


# ---- 点名场景：同位置并发插入按 id 字典序 ------------------------------
def test_same_position_inserts_lexicographic():
    base = "ABCDEF"
    ops = [
        ot.make_ins(3, "x", "p-a"),
        ot.make_ins(3, "y", "p-b"),
        ot.make_ins(3, "z", "p-c"),
    ]
    final = assert_all_orders_convergent(base, ops)
    assert final == "ABCxyzDEF", final


def test_same_position_order_independent_of_arrival():
    base = "A"
    ops = [ot.make_ins(1, "2", "id-z"),
           ot.make_ins(1, "1", "id-a"),
           ot.make_ins(1, "x", "id-m")]
    final = assert_all_orders_convergent(base, ops)
    assert final == "A1x2", final


# ---- 点名场景：删除区间内插入必须保留 ----------------------------------
def test_insert_inside_delete_is_preserved():
    base = "0123456789"
    d = ot.make_del(2, 7, "del-mid")
    i = ot.make_ins(4, "KEEP", "ins-mid")
    final = assert_pair_convergent(base, d, i)
    assert final == "01KEEP789", final


def test_insert_at_delete_boundaries():
    base = "0123456789"
    d = ot.make_del(2, 6, "D")  # 删除 "2345"
    assert_pair_convergent(base, d, ot.make_ins(2, "L", "L"))
    assert_pair_convergent(base, d, ot.make_ins(6, "R", "R"))
    assert_pair_convergent(base, d, ot.make_ins(0, "H", "H"))
    assert_pair_convergent(base, d, ot.make_ins(10, "T", "T"))
    # 左界插入收敛结果
    assert assert_pair_convergent(base, d, ot.make_ins(2, "L", "L")) == "01L6789"
    assert assert_pair_convergent(base, d, ot.make_ins(6, "R", "R")) == "01R6789"


# ---- 点名场景：重叠删除取并集 ------------------------------------------
def test_overlapping_deletes_union():
    base = "0123456789"
    pairs = [
        (ot.make_del(1, 5, "d1"), ot.make_del(3, 8, "d2"), "089"),
        (ot.make_del(0, 10, "a", ), ot.make_del(3, 4, "b"), ""),
        (ot.make_del(2, 4, "a"), ot.make_del(6, 8, "b"), "014589"),
        (ot.make_del(2, 7, "a"), ot.make_del(4, 6, "b"), "01789"),
        (ot.make_del(4, 6, "a"), ot.make_del(2, 7, "b"), "01789"),
    ]
    for a, b, expected in pairs:
        assert assert_pair_convergent(base, a, b) == expected, (a, b)


def test_three_overlapping_deletes_all_orders():
    base = "0123456789"
    ops = [ot.make_del(1, 5, "d1"),
           ot.make_del(3, 8, "d2"),
           ot.make_del(7, 9, "d3")]
    # 纯删除并集与路径无关，任意排列都收敛
    final = assert_all_orders_convergent(base, ops)
    assert final == "09", final


# ---- 插入跨越删除（插入区间概念：多点位）逐对穷举 ----------------------
def test_all_region_combinations_exhaustive():
    base = "0123456789"  # 长度 10，11 个插入位
    # 插入 vs 删除：插入点取遍所有位置，删除区间取遍所有非空子区间
    for p in range(11):
        for lo in range(10):
            for hi in range(lo + 1, 11):
                assert_pair_convergent(
                    base, ot.make_ins(p, "X", f"ins-{p}"),
                    ot.make_del(lo, hi, f"del-{lo}-{hi}"))
    # 删除 vs 删除：取遍所有区间对（含空区间）
    for a_lo in range(11):
        for a_hi in range(a_lo, 11):
            for b_lo in range(11):
                for b_hi in range(b_lo, 11):
                    assert_pair_convergent(
                        base, ot.make_del(a_lo, a_hi, "a"),
                        ot.make_del(b_lo, b_hi, "b"))


def test_random_pairs_convergence():
    rng = random.Random(20260925)
    alphabet = "甲乙丙丁ABC012"
    for _ in range(2000):
        base = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        n = len(base)
        ops = []
        for k in range(2):
            if rng.random() < 0.5:
                ops.append(ot.make_ins(rng.randint(0, n),
                                       rng.choice("XY插入"), f"p-{k}"))
            else:
                lo = rng.randint(0, n)
                ops.append(ot.make_del(lo, rng.randint(lo, n), f"p-{k}"))
        assert_pair_convergent(base, ops[0], ops[1])


# ---- 固定服务端顺序下多补丁结果确定且等于逐字符并集语义 ----------------
def test_multi_patch_deterministic_under_server_order():
    # 三台终端基于同一旧修订并发，按某个固定服务端顺序依次到达
    base = "0123456789"
    ops = [
        ot.make_del(2, 8, "D"),
        ot.make_ins(5, "K", "ins-in"),
        ot.make_del(4, 7, "d2"),
        ot.make_ins(9, "Z", "z-tail"),
    ]
    text1, _ = simulate_order(base, ops, (0, 1, 2, 3))
    text2, _ = simulate_order(base, ops, (0, 1, 2, 3))
    assert text1 == text2
    # 删除字符并集 [2,8) 一定消失，K 与 Z 一定保留
    assert set("234567") - set(text1) == set("234567")
    assert "K" in text1 and "Z" in text1


def test_empty_delete_is_noop():
    base = "abc"
    final = assert_pair_convergent(base, ot.make_del(1, 1, "e"),
                                   ot.make_ins(1, "X", "i"))
    assert final in ("aXbc", "aXbc"), final
    t, _ = simulate_order(base, [ot.make_del(1, 1, "e")], (0,))
    assert t == base
