#!/usr/bin/env python3
"""零依赖测试发现/运行器：收集 tests/ 下 test_*.py 中的 test_* 函数。

若环境装有 pytest 也可直接用 pytest；本运行器不依赖任何第三方包。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback


def discover(path: str):
    tests = []
    for name in sorted(os.listdir(path)):
        if name.startswith("test_") and name.endswith(".py"):
            mod_name = name[:-3]
            spec = importlib.util.spec_from_file_location(mod_name, os.path.join(path, name))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for fn in sorted(dir(module)):
                if fn.startswith("test_") and callable(getattr(module, fn)):
                    tests.append((f"{mod_name}::{fn}", getattr(module, fn)))
    return tests


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    tests = discover(os.path.join(root, "tests"))
    failures = []
    for label, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures.append((label, traceback.format_exc()))
            print(f"FAIL {label}")
        else:
            print(f"PASS {label}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} 通过")
    for label, tb in failures:
        print("\n" + "=" * 70 + f"\n{label}\n" + "=" * 70)
        print(tb)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
