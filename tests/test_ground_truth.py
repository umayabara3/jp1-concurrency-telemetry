"""「枠待ちの疑い（推定）」がどれだけ当たるかを、正解の分かるシミュレーションで測る。

サンプルはシミュレーションで作っているので、各ジョブが本当に待ったか（起動可能＜開始）が分かる。
実行ログ（開始・終了だけ）から出した推定と、その正解を突き合わせる。
    python tests/test_ground_truth.py
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import jp1_concurrency as jc  # noqa: E402
import make_sample as ms  # noqa: E402


def truth(seed: int, load: float, jitter: bool, base: dt.datetime, cap: int = 20) -> set[tuple[str, str]]:
    jobs = ms.build_jobnet(random.Random(seed), load, jitter)
    start, _, ready = ms.simulate(jobs, cap)
    return {(jobs[i]["name"], f"{base + dt.timedelta(seconds=start[i]):%Y/%m/%d %H:%M:%S}")
            for i in jobs if start[i] > ready[i]}


def estimate(path: Path, cap: int = 20) -> set[tuple[str, str]]:
    r = jc.analyze(path.read_bytes(), jc.Options(limit=cap))
    return {(j.name, jc._dt(j.s)) for j, s in zip(r["jobs"], r["suspect"]) if s}


def score(t: set, e: set) -> tuple[float, float]:
    tp = len(t & e)
    return (tp / len(e) if e else 1.0), (tp / len(t) if t else 1.0)


def test_one_night_exact() -> None:
    t = truth(20261105, 1.0, False, dt.datetime(2026, 11, 4, 22))
    e = estimate(ROOT / "samples" / "sample_1day_100jobs.csv")
    p, r = score(t, e)
    print(f"1晩: 実際に待った {len(t)} 本 / 推定 {len(e)} 本  適合率 {p:.0%}  再現率 {r:.0%}")
    assert t == e


def test_five_nights() -> None:
    plan = [(dt.datetime(2026, 10, 31, 22), 1031, 1.0, True), (dt.datetime(2026, 11, 1, 22), 1101, 0.9, True),
            (dt.datetime(2026, 11, 2, 22), 1102, 1.6, True), (dt.datetime(2026, 11, 3, 22), 1103, 1.05, True),
            (dt.datetime(2026, 11, 4, 22), 20261105, 1.0, False)]
    t = set().union(*(truth(seed, load, jit, base) for base, seed, load, jit in plan))
    t.discard(("日次終了処理", "2026/11/04 01:32:32"))   # サンプルで「実行中」に書き換えた行（ログから除外される）
    e = estimate(ROOT / "samples" / "sample_5days.csv")
    p, r = score(t, e)
    print(f"5晩: 実際に待った {len(t)} 本 / 推定 {len(e)} 本  適合率 {p:.0%}  再現率 {r:.0%}")
    assert p >= 0.95 and r >= 0.95


if __name__ == "__main__":
    test_one_night_exact()
    test_five_nights()
