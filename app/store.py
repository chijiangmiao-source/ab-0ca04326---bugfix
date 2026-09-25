"""规程文档的持久化存储：全文、连续修订号、补丁历史原子落盘。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

DEFAULT_TEXT = (
    "低温联锁系统操作规程\n"
    "一、启车前须确认联锁回路处于投用状态。\n"
    "二、联锁触发后按现场处置卡执行，并逐项确认。\n"
)


class Store:
    """以单个 JSON 文件原子保存全部状态。

    状态结构::

        {"text": str, "revision": int,
         "patches": [ {id, base_revision, revision, kind, payload, pieces}, ... ]}

    每次确认都把 "新全文 / 新修订号 / 本补丁规范化结果" 在同一次
    原子替换（临时文件 + os.replace）中落盘，任何拒绝都不触发写盘。
    """

    def __init__(self, path: str, initial_text: str = DEFAULT_TEXT) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self._state: Dict[str, Any] = json.load(fh)
        else:
            self._state = {"text": initial_text, "revision": 0, "patches": []}
            self._flush()

    # ---- 内部 ----------------------------------------------------------
    def _flush(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- 读取 ----------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"text": self._state["text"],
                    "revision": self._state["revision"]}

    def find_patch(self, patch_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for rec in self._state["patches"]:
                if rec["id"] == patch_id:
                    return json.loads(json.dumps(rec))
            return None

    def find_patch_by_revision(self, revision: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for rec in self._state["patches"]:
                if rec["revision"] == revision:
                    return json.loads(json.dumps(rec))
            return None

    def initial_text(self) -> str:
        """修订号 0 对应的全文（无补丁时为当前文本，否则取首条补丁基准）。"""
        with self._lock:
            if not self._state["patches"]:
                return self._state["text"]
            return self._state["patches"][0]["base_text"]

    def history_since(self, base_revision: int) -> List[List[List[Any]]]:
        """返回修订号 > base_revision 的各补丁规范化片段（按修订顺序）。"""
        with self._lock:
            out: List[List[List[Any]]] = []
            for rec in self._state["patches"]:
                if rec["revision"] > base_revision:
                    out.append(json.loads(json.dumps(rec["pieces"])))
            return out

    # ---- 写入 ----------------------------------------------------------
    def commit(self, record: Dict[str, Any], new_text: str) -> None:
        """原子保存新全文、新修订号与本补丁规范化结果。"""
        with self._lock:
            self._state["text"] = new_text
            self._state["revision"] = record["revision"]
            self._state["patches"].append(record)
            self._flush()
