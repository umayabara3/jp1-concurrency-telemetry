"""JS（jp1-telemetry.html の CORE）と Python（tools/jp1_concurrency.py）の計算結果を突合する。

    python tests/test_crosscheck.py      # pytest でも実行可
同じ入力・同じ設定で、多重度の階段・ピーク区間・スロット・枠待ちの疑い・ヒートマップまで一致することを確認する。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import jp1_concurrency as jc  # noqa: E402

FILES = ["samples/sample_1day_100jobs.csv", "samples/sample_5days.csv", *sorted(
    str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "fixtures").iterdir())]
OPTIONS = [
    dict(limit=20),
    dict(limit=0, long_mode="pct", long_val=10),
    dict(limit=15, long_mode="min", long_val=10, boundary=18, filter="取込, 集計"),
    dict(limit=20, day="2026-11-02"),
    dict(limit=20, filter="!日付変更後"),
]


def js_summary(path: str, opt: dict) -> dict:
    out = subprocess.run(["node", str(ROOT / "tests" / "summary.js"), str(ROOT / path), json.dumps(opt)],
                         check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


def py_summary(path: str, opt: dict) -> dict:
    o = jc.Options(limit=opt.get("limit", 0), long_mode=opt.get("long_mode", "min"), long_val=opt.get("long_val", 20),
                   boundary=opt.get("boundary", 12), day=opt.get("day", "all"), filter=opt.get("filter", ""))
    s = jc.summary_json(jc.analyze((ROOT / path).read_bytes(), o))
    return json.loads(json.dumps(s, default=str).replace("NaN", "null").replace("Infinity", "null"))


def same(a, b, where="") -> None:
    if isinstance(a, float) or isinstance(b, float):
        assert a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6), f"{where}: {a} != {b}"
    elif isinstance(a, dict):
        assert set(a) == set(b), f"{where}: keys {set(a) ^ set(b)}"
        for k in a:
            same(a[k], b[k], f"{where}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), f"{where}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            same(x, y, f"{where}[{i}]")
    else:
        assert a == b, f"{where}: {a!r} != {b!r}"


def test_crosscheck() -> None:
    n = 0
    for f in FILES:
        for opt in OPTIONS:
            if "day" in opt and "5days" not in f:
                continue
            j, p = js_summary(f, opt), py_summary(f, opt)
            same(j, p, f"{f} {opt}")
            n += 1
            print(f"ok {f} {opt}  peak={p['stats']['peak']} segments={len(p['series'])} suspects={len(p['suspects'])}")
    assert n >= 20
    print(f"\n{n} combinations matched")


if __name__ == "__main__":
    test_crosscheck()
