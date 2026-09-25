#!/usr/bin/env python3
"""Compose verify 服务的验收脚本。

按固定顺序把业务复核与代码测试、构建检查、接口冒烟穿插执行：

  1. 接口冒烟（健康检查 / 页面 / 静态资源，等待 compose app 就绪）
  2. 业务复核 A：同位置并发插入的收敛文本（两种提交序分别打到两台
     全新临时实例，断言逐字符相同且等于人工预期）
  3. 代码测试（scripts/run_tests.py，全部 OT/服务/持久化用例）
  4. 构建检查（Python compileall；node --check 校验前端 JS 语法）
  5. 接口冒烟（草案/文档/补丁 happy path 与 404/400）
  6. 业务复核 B：删除区间内并发插入的保留结果（两种提交序收敛）
  7. 重启持久化冒烟（停服再起：历史转换旧修订补丁、幂等不增修订）
  8. 业务复核 C：拒绝提交后全文与修订号逐字节不变（打 compose app）

任何一步失败：继续执行剩余检查以便完整报告，但最终以非零退出。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
APP_URL = os.environ.get("APP_URL", "http://app:8000")

_failures: list[str] = []


def step(no: int, title: str):
    print("\n" + "=" * 72)
    print(f"步骤 {no}：{title}")
    print("=" * 72, flush=True)


def check(cond: bool, msg: str):
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        _failures.append(msg)


def http(method: str, url: str, body=None, timeout: int = 10):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    def parse(raw: str, resp_headers):
        ctype = resp_headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(raw) if raw else None
        return raw

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), parse(raw, resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = parse(raw, exc.headers)
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, dict(exc.headers), parsed


def wait_healthy(url: str, attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            status, _, body = http("GET", f"{url}/healthz", timeout=3)
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    return False


@contextmanager
def local_server(port: int, data_dir: str):
    env = dict(os.environ)
    env.update({"PORT": str(port), "HOST": "127.0.0.1",
                "DATA_FILE": os.path.join(data_dir, "state.json"),
                "PYTHONPATH": ROOT})
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_healthy(f"http://127.0.0.1:{port}", attempts=30):
            raise RuntimeError(f"临时实例 :{port} 未就绪")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def fresh_dir() -> str:
    return tempfile.mkdtemp(prefix="verify-")


def post_patch(base: str, pid, kind, payload, rev=0):
    return http("POST", f"{base}/api/patches",
                {"id": pid, "kind": kind, "base_revision": rev,
                 "payload": payload})


# ----------------------------------------------------------------------
def phase1_smoke_static():
    step(1, "接口冒烟：健康检查、页面与静态资源")
    check(wait_healthy(APP_URL), f"compose app {APP_URL}/healthz 返回 ok")

    status, headers, _ = http("GET", f"{APP_URL}/")
    check(status == 200 and "text/html" in headers.get("Content-Type", ""),
          "GET / 返回 200 text/html")
    status, headers, body = http("GET", f"{APP_URL}/app.js")
    js_ok = status == 200 and "javascript" in headers.get("Content-Type", "") \
        and isinstance(body, str) and "use strict" in body
    check(js_ok, "GET /app.js 返回 200 且内容为前端脚本")
    status, _, _ = http("GET", f"{APP_URL}/style.css")
    check(status == 200, "GET /style.css 返回 200")
    status, _, body = http("GET", f"{APP_URL}/api/document")
    check(status == 200 and isinstance(body, dict)
          and set(body) == {"text", "revision"},
          "GET /api/document 返回 {text, revision}")


def phase2_concurrent_insert():
    step(2, "业务复核 A：同位置并发插入按 id 字典序收敛（两种提交序）")
    from app.store import DEFAULT_TEXT as base_text
    pos = 4
    expected = base_text[:pos] + "[A][B]" + base_text[pos:]

    results = {}
    for order, port, first, second in (
            ("ab", 18001, ("a", "[A]"), ("b", "[B]")),
            ("ba", 18002, ("b", "[B]"), ("a", "[A]"))):
        d = fresh_dir()
        with local_server(port, d) as base:
            s, _, draft = http("POST", f"{base}/api/drafts")
            check(s == 201 and draft["base_revision"] == 0,
                  f"[{order}] 建立草案成功，基准修订 0")
            # 两台离线终端基于同一修订 0，提交先后不同
            for pid, text in (first, second):
                st, _, r = post_patch(base, f"biz-ins-{pid}", "insert",
                                      {"pos": pos, "text": text}, rev=0)
                check(st == 200 and r["revision"] in (1, 2),
                      f"[{order}] 补丁 {pid} 被确认，修订连续")
            _, _, doc = http("GET", f"{base}/api/document")
            results[order] = doc["text"]
            check(doc["revision"] == 2, f"[{order}] 共产生 2 个连续修订")
        shutil.rmtree(d, ignore_errors=True)

    check(results["ab"] == results["ba"] == expected,
          "无论提交先后，收敛文本逐字符一致且为人工预期："
          f"{results['ab']!r}")


def phase3_code_tests():
    step(3, "代码测试：OT 变换 / 业务规则 / 持久化（全部用例）")
    proc = subprocess.run([sys.executable, "scripts/run_tests.py"],
                          cwd=ROOT, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout else ""
    print(proc.stdout)
    if proc.stderr.strip():
        print(proc.stderr)
    check(proc.returncode == 0 and "通过" in tail,
          f"代码测试全部通过（{tail}）")


def phase4_build_checks():
    step(4, "构建检查：Python 字节码编译 + 前端 JS 语法")
    py = subprocess.run([sys.executable, "-m", "compileall", "-q",
                         "app", "scripts"], cwd=ROOT,
                        capture_output=True, text=True)
    check(py.returncode == 0,
          "python -m compileall app scripts 通过（无语法错误）")
    if shutil.which("node"):
        js = subprocess.run(["node", "--check", "app/static/app.js"],
                            cwd=ROOT, capture_output=True, text=True)
        check(js.returncode == 0, "node --check app/static/app.js 通过")
    else:
        check(False, "node 可用（用于前端 JS 语法检查）")


def phase5_smoke_api():
    step(5, "接口冒烟：草案 → 补丁 happy path、404、非法 JSON")
    s, _, draft = http("POST", f"{APP_URL}/api/drafts")
    check(s == 201 and draft["base_text"] is not None
          and isinstance(draft["base_revision"], int),
          "POST /api/drafts 201，返回当前全文与基准修订号")
    s, _, doc = http("GET", f"{APP_URL}/api/document")
    check(s == 200 and doc["text"] == draft["base_text"]
          and doc["revision"] == draft["base_revision"],
          "草案内容与 /api/document 完全一致")
    s, _, body = http("GET", f"{APP_URL}/no-such-path")
    check(s == 404, "未知路径返回 404")
    # 非法 JSON → 400
    req = urllib.request.Request(
        f"{APP_URL}/api/patches", data=b"{not-json",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        check(False, "非法 JSON 应返回 400")
    except urllib.error.HTTPError as exc:
        check(exc.code == 400, f"非法 JSON 返回 400（实际 {exc.code}）")


def phase6_insert_inside_delete():
    step(6, "业务复核 B：删除区间内并发插入必须保留（两种提交序）")
    from app.store import DEFAULT_TEXT as base_text
    lo, hi, in_pos, marker = 2, 7, 4, "★保留★"
    expected = base_text[:lo] + marker + base_text[hi:]
    removed = base_text[lo:hi]

    results = {}
    for order, seq in (("del-first", ("del", "ins")),
                       ("ins-first", ("ins", "del"))):
        d = fresh_dir()
        with local_server(18003 if order == "del-first" else 18004, d) as base:
            for kind in seq:
                if kind == "del":
                    st, _, r = post_patch(base, "biz-del", "delete",
                                          {"lo": lo, "hi": hi, "length": hi - lo},
                                          rev=0)
                else:
                    st, _, r = post_patch(base, "biz-ins", "insert",
                                          {"pos": in_pos, "text": marker}, rev=0)
                check(st == 200, f"[{order}] {kind} 补丁确认成功")
            _, _, doc = http("GET", f"{base}/api/document")
            results[order] = doc["text"]
        shutil.rmtree(d, ignore_errors=True)

    check(results["del-first"] == results["ins-first"] == expected,
          "两种提交序收敛到同一保留结果")
    check(marker in results["del-first"] and removed not in results["del-first"],
          f"插入内容 {marker} 保留，且被删连续片段 {removed!r} 不再存在"
          "（后文若有相同单字属正常，故按连续片段判定）")


def phase7_restart_persistence():
    step(7, "重启持久化冒烟：旧修订补丁历史转换、幂等不增修订")
    d = fresh_dir()
    with local_server(18005, d) as base:
        st, _, r1 = post_patch(base, "keep-a", "insert",
                               {"pos": 5, "text": "K"}, rev=0)
        check(st == 200 and r1["revision"] == 1, "首次补丁确认（修订 1）")
    # 停服后用同一数据文件重启
    with local_server(18005, d) as base:
        _, _, doc = http("GET", f"{base}/api/document")
        check(doc["revision"] == 1 and "K" in doc["text"],
              "重启后全文与修订号从磁盘恢复")
        # 基于旧修订 0 的合法迟到删除：越过修订 1 做历史转换
        st, _, r2 = post_patch(base, "keep-late", "delete",
                               {"lo": 4, "hi": 7}, rev=0)
        check(st == 200 and r2["revision"] == 2 and len(r2["pieces"]) >= 1,
              "重启后仍可转换旧修订 0 上的合法补丁（修订 2）")
        # 同 id 同载荷重传：复现首次结果，不新增修订
        st, _, r3 = post_patch(base, "keep-a", "insert",
                               {"pos": 5, "text": "K"}, rev=0)
        check(st == 200 and r3["idempotent"] is True
              and r3["revision"] == 1,
              "相同载荷重传幂等：复现修订 1，未新增修订")
        _, _, doc2 = http("GET", f"{base}/api/document")
        check(doc2["revision"] == 2, "当前修订仍为 2")
    shutil.rmtree(d, ignore_errors=True)


def phase8_rejection_leaves_unchanged():
    step(8, "业务复核 C：拒绝提交后全文与修订号保持不变（compose app）")
    _, _, before = http("GET", f"{APP_URL}/api/document")
    bad_cases = [
        ("未来修订", {"id": f"bad-future-{time.time_ns()}", "kind": "insert",
                      "base_revision": before["revision"] + 100,
                      "payload": {"pos": 0, "text": "X"}}),
        ("插入越界", {"id": f"bad-oob-{time.time_ns()}", "kind": "insert",
                     "base_revision": before["revision"],
                     "payload": {"pos": len(before["text"]) + 50, "text": "X"}}),
        ("长度不符", {"id": f"bad-len-{time.time_ns()}", "kind": "delete",
                     "base_revision": before["revision"],
                     "payload": {"lo": 0, "hi": 3, "length": 9}}),
        ("删除越界", {"id": f"bad-doob-{time.time_ns()}", "kind": "delete",
                     "base_revision": before["revision"],
                     "payload": {"lo": len(before["text"]),
                                 "hi": len(before["text"]) + 2}}),
    ]
    for label, payload in bad_cases:
        st, _, body = http("POST", f"{APP_URL}/api/patches", payload)
        check(st in (400, 409), f"非法提交（{label}）被拒绝，HTTP {st}")
        if isinstance(body, dict):
            check(body.get("revision") == before["revision"]
                  and body.get("text") == before["text"],
                  f"拒绝响应（{label}）回显的全文与修订号未变")
    _, _, after = http("GET", f"{APP_URL}/api/document")
    check(after == before,
          "拒绝全部尝试后，GET /api/document 全文与修订号逐字节不变"
          f"（修订 {after['revision']} == {before['revision']}）")

    # 标识复用但载荷不同：在临时实例上先占一个 id，再以不同载荷复用
    d = fresh_dir()
    with local_server(18006, d) as base:
        st, _, r = post_patch(base, "fixed-id", "insert",
                              {"pos": 0, "text": "FIRST"}, rev=0)
        check(st == 200 and r["revision"] == 1, "标识 fixed-id 首次占用成功")
        _, _, mid = http("GET", f"{base}/api/document")
        st, _, body = post_patch(base, "fixed-id", "delete",
                                 {"lo": 0, "hi": 1}, rev=0)
        check(st == 409 and isinstance(body, dict)
              and body.get("error") == "id_reused",
              "同标识不同载荷被拒绝（409 id_reused）")
        _, _, unchanged = http("GET", f"{base}/api/document")
        check(unchanged == mid, "标识复用被拒绝后规程文本与修订号不变")
        # 同标识同载荷重传仍幂等
        st, _, again = post_patch(base, "fixed-id", "insert",
                                  {"pos": 0, "text": "FIRST"}, rev=0)
        check(st == 200 and again["idempotent"] is True
              and again["revision"] == 1,
              "随后相同载荷重传幂等成功，仍复现修订 1")
    shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    phase1_smoke_static()
    phase2_concurrent_insert()
    phase3_code_tests()
    phase4_build_checks()
    phase5_smoke_api()
    phase6_insert_inside_delete()
    phase7_restart_persistence()
    phase8_rejection_leaves_unchanged()

    print("\n" + "=" * 72)
    if _failures:
        print(f"验收失败：{len(_failures)} 项未通过")
        for msg in _failures:
            print(f"  - {msg}")
        print("=" * 72)
        return 1
    print("验收全部通过：业务收敛/保留/拒绝不变性、代码测试、构建检查、"
          "接口冒烟与重启持久化均通过。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
