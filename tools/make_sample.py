"""サンプル実行ログ（JP1 実行履歴風 CSV）を生成する。

ジョブネット 100 ジョブを多重度上限つきでシミュレーションし、
実行ログと同じ「ジョブ名・開始日時・終了日時・経過秒」の形で書き出す。
標準ライブラリのみ。乱数シード固定なので何度実行しても同じファイルになる。

    python tools/make_sample.py            # samples/ に 2 ファイルを生成
"""
from __future__ import annotations

import csv
import datetime as dt
import heapq
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["ジョブネット名", "ジョブ名", "開始日時", "終了日時", "経過時間(秒)", "終了状態"]
NET = {
    "開始・終了": "/日次バッチ",
    "取込": "/日次バッチ/取込",
    "集計": "/日次バッチ/集計",
    "帳票": "/日次バッチ/帳票",
    "00:00起動": "/日次バッチ/日付変更後",
    "クリティカル系列": "/日次バッチ/DWH連携",
}


def build_jobnet(rng: random.Random, load: float = 1.0, jitter: bool = True) -> dict:
    """100 ジョブの定義（所要秒・先行ジョブ・時刻起動）を作る。

    load は取込・マスタ更新の重さ倍率。jitter=False で大型マスタ更新を 80 分固定にする。
    """

    def ln(median: float, sigma: float = 0.45) -> int:
        return int(round(max(60, median * math.exp(rng.gauss(0, sigma)))))

    jobs: dict[int, dict] = {}

    def add(i, name, grp, dur, deps, not_before=0):
        jobs[i] = dict(name=name, grp=grp, dur=dur, deps=deps, nb=not_before)

    add(1, "日次開始処理", "開始・終了", 120, [])
    for i in range(2, 32):
        add(i, f"受信取込{i - 1:02d}", "取込", int(ln(540) * load), [1])
    master = 4800 * (0.9 + 0.2 * rng.random()) if jitter else 4800
    add(32, "大型マスタ更新", "クリティカル系列", int(master * load), [1])
    for i in range(33, 63):
        add(i, f"集計{i - 32:02d}", "集計", ln(600), rng.sample(range(2, 32), rng.choice([1, 1, 2])))
    for i in range(63, 76):
        add(i, f"帳票出力{i - 62:02d}", "帳票", ln(480), rng.sample(range(33, 63), 2))
    for i in range(76, 96):
        add(i, f"日付変更後処理{i - 75:02d}", "00:00起動", ln(780, 0.35), [1], not_before=2 * 3600)
    add(96, "差分抽出", "クリティカル系列", 1800, [32])
    add(97, "DWH連携", "クリティカル系列", 2400, [96])
    add(98, "整合性チェック", "クリティカル系列", 1500, [97])
    add(99, "バックアップ", "クリティカル系列", 1200, [98])
    add(100, "日次終了処理", "開始・終了", 90, list(range(63, 96)) + [99])
    return jobs


def simulate(jobs: dict, cap: int) -> tuple[dict, dict, dict]:
    """多重度上限 cap で投入順（起動可能になった順→番号順）に実行したときの開始・終了・起動可能秒。"""
    t = 0
    done: dict[int, int] = {}
    start: dict[int, int] = {}
    running: list[tuple[int, int]] = []
    waiting: list[tuple[int, int]] = []
    queued: set[int] = set()
    ready: dict[int, int] = {}
    pending = set(jobs)

    def ready_time(i):
        j = jobs[i]
        if not all(d in done for d in j["deps"]):
            return None
        return max([done[d] for d in j["deps"]] + [j["nb"]])

    def enqueue(now):
        for i in sorted(pending - queued):
            rt = ready_time(i)
            if rt is not None and rt <= now:
                queued.add(i)
                ready[i] = rt
                waiting.append((rt, i))

    enqueue(0)
    while pending:
        waiting.sort()
        while waiting and len(running) < cap:
            _, i = waiting.pop(0)
            start[i] = t
            heapq.heappush(running, (t + jobs[i]["dur"], i))
            pending.discard(i)
        cands = [running[0][0]] if running else []
        for i in pending - queued:
            rt = ready_time(i)
            if rt is not None:
                cands.append(rt)
        t = min(cands)
        while running and running[0][0] <= t:
            e, i = heapq.heappop(running)
            done[i] = e
        enqueue(t)
    while running:
        e, i = heapq.heappop(running)
        done[i] = e
    return start, done, ready


def rows_for_night(base: dt.datetime, seed: int, cap: int = 20, load: float = 1.0,
                   jitter: bool = True) -> list[list[str]]:
    rng = random.Random(seed)
    jobs = build_jobnet(rng, load, jitter)
    start, done, _ = simulate(jobs, cap)
    rows = []
    for i in sorted(jobs, key=lambda k: (start[k], k)):
        j = jobs[i]
        s = base + dt.timedelta(seconds=start[i])
        e = base + dt.timedelta(seconds=done[i])
        rows.append([NET[j["grp"]], j["name"], f"{s:%Y/%m/%d %H:%M:%S}", f"{e:%Y/%m/%d %H:%M:%S}",
                     str(done[i] - start[i]), "正常終了"])
    return rows


def write(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)} ({len(rows)} rows)")


def main() -> None:
    out = ROOT / "samples"
    out.mkdir(exist_ok=True)
    # 1 晩分：会話で示した「100 ジョブ・多重度 20」のサンプル
    write(out / "sample_1day_100jobs.csv",
          rows_for_night(dt.datetime(2026, 11, 4, 22, 0, 0), 20261105, jitter=False))

    # 5 晩分：月曜（11/02）は週末分の取込で重い想定。11/03 は実行中（終了日時なし）の行を 1 件含める
    rows: list[list[str]] = []
    plan = [(dt.datetime(2026, 10, 31), 1031, 1.0, True), (dt.datetime(2026, 11, 1), 1101, 0.9, True),
            (dt.datetime(2026, 11, 2), 1102, 1.6, True), (dt.datetime(2026, 11, 3), 1103, 1.05, True),
            (dt.datetime(2026, 11, 4), 20261105, 1.0, False)]
    for day, seed, load, jitter in plan:
        night = rows_for_night(day.replace(hour=22), seed, load=load, jitter=jitter)
        if day.day == 3:
            night[-1][3] = ""
            night[-1][4] = ""
            night[-1][5] = "実行中"
        rows.extend(night)
    write(out / "sample_5days.csv", rows)


if __name__ == "__main__":
    main()
