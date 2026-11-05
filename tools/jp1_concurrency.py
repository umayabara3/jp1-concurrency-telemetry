"""JP1 実行履歴から多重度（同時実行ジョブ数）のピークを分析する CLI。標準ライブラリのみ。

jp1-telemetry.html と同じ規則で計算し、レポート（Markdown）と CSV を書き出す。
JP1 の後続ジョブとして毎晩実行すれば、AI もブラウザも使わずに日次レポートが残る。

    python tools/jp1_concurrency.py 実行履歴.csv --limit 20
    python tools/jp1_concurrency.py 実行履歴.csv --limit 20 --day 2026-11-04 --out report
    python tools/jp1_concurrency.py 実行履歴.csv --map "start=開始日時,end=終了日時,name=ジョブ名"
    python tools/jp1_concurrency.py 実行履歴.csv --png      # matplotlib があれば PNG も出す

出力（--out 既定は ./jp1-report-<入力ファイル名>）:
    report.md     要約・ピーク区間・長時間ジョブ・業務日×時刻の最大多重度
    timeline.csv  多重度の階段（区間ごとの実行中本数）
    jobs.csv      ジョブ別（長時間判定・開始直前の多重度・スロット・枠待ちの疑い）
    peaks.csv     ピーク区間
    hourly.csv    業務日×時刻の最大多重度
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import csv
import heapq
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DAY = 86400
WD = "月火水木金土日"  # Python の weekday(): 月=0

# ---------------------------------------------------------------- 読み込み


def decode_bytes(b: bytes, enc: str = "auto") -> tuple[str, str]:
    if enc == "auto":
        if b[:3] == b"\xef\xbb\xbf":
            return b[3:].decode("utf-8"), "utf-8"
        if b[:2] == b"\xff\xfe":
            return b[2:].decode("utf-16-le"), "utf-16le"
        if b[:2] == b"\xfe\xff":
            return b[2:].decode("utf-16-be"), "utf-16be"
        try:
            return b.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return b.decode("cp932", errors="replace"), "shift_jis"
    name = {"shift_jis": "cp932", "sjis": "cp932"}.get(enc.lower(), enc)
    text = b.decode(name, errors="replace")
    return text.lstrip("﻿"), enc


def _count_outside_quotes(line: str, d: str) -> int:
    q, c = False, 0
    for ch in line:
        if ch == '"':
            q = not q
        elif not q and ch == d:
            c += 1
    return c


def detect_delimiter(text: str) -> str:
    lines = [ln for ln in re.split(r"\r?\n", text) if ln.strip()][:40]
    best, best_score = None, 0.0
    for d in [",", "\t", ";", "|"]:
        nz = [c for c in (_count_outside_quotes(ln, d) for ln in lines) if c > 0]
        if not nz or len(nz) < len(lines) * 0.6:
            continue
        freq: dict[int, int] = {}
        for c in nz:
            freq[c] = freq.get(c, 0) + 1
        mode, mf = 0, 0
        for c, f in freq.items():
            if f > mf or (f == mf and c > mode):
                mf, mode = f, c
        score = (mf / len(lines)) * min(mode, 30)
        if score > best_score:
            best_score, best = score, d
    return best or "ws"


def split_rows(text: str, delim: str) -> list[list[str]]:
    if delim == "ws":
        return [re.split(r"\t| {2,}|　+", ln.strip()) for ln in re.split(r"\r?\n", text) if ln.strip()]
    rows: list[list[str]] = []
    row: list[str] = []
    f, q, quoted, i, n = "", False, False, 0, len(text)
    while i < n:
        c = text[i]
        if q:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    f += '"'
                    i += 2
                    continue
                q = False
                i += 1
                continue
            f += c
            i += 1
            continue
        if c == '"' and f.strip() == "" and not quoted:
            q, quoted, f = True, True, ""
            i += 1
            continue
        if c == delim:
            row.append(f)
            f, quoted = "", False
            i += 1
            continue
        if c == "\r":
            i += 1
            continue
        if c == "\n":
            row.append(f)
            rows.append(row)
            row, f, quoted = [], "", False
            i += 1
            continue
        f += c
        i += 1
    if f != "" or row:
        row.append(f)
        rows.append(row)
    return [r for r in rows if any(x.strip() != "" for x in r)]


D = "[0-9]"
RE_DT = re.compile(rf"^\s*({D}{{4}})[/\-.年]({D}{{1,2}})[/\-.月]({D}{{1,2}})日?(?:[ T　_]+({D}{{1,2}})[:時]({D}{{1,2}})(?:[:分]({D}{{1,2}})(?:[.,]({D}{{1,6}}))?秒?)?)?")
RE_COMPACT = re.compile(rf"^\s*({D}{{4}})({D}{{2}})({D}{{2}})[ T]?({D}{{2}})({D}{{2}})({D}{{2}})?(?:[^0-9]|$)")
RE_TOD = re.compile(rf"^\s*({D}{{1,2}})[:時]({D}{{1,2}})(?:[:分]({D}{{1,2}})(?:[.,]({D}{{1,6}}))?秒?)?\s*$")


def _match_dt(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return RE_DT.match(s) or RE_COMPACT.match(s)


def parse_datetime(s) -> float:
    m = _match_dt(s)
    if not m:
        return math.nan
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    H = int(m[4]) if m[4] is not None else 0
    Mi = int(m[5]) if m[5] is not None else 0
    S = int(m[6]) if m[6] is not None else 0
    frac = float("0." + m[7]) if m[7] else 0.0
    if mo < 1 or mo > 12 or d < 1 or d > 31 or H > 24 or Mi > 59 or S > 60:
        return math.nan
    return calendar.timegm((y, mo, d, H, Mi, S, 0, 0, 0)) + frac


def has_time_part(s) -> bool:
    m = _match_dt(s)
    return bool(m and m[4] is not None)


def parse_time_of_day(s) -> float:
    m = RE_TOD.match("" if s is None else str(s))
    if not m:
        return math.nan
    H, Mi = int(m[1]), int(m[2])
    S = int(m[3]) if m[3] is not None else 0
    if H > 47 or Mi > 59 or S > 60:
        return math.nan
    return H * 3600 + Mi * 60 + S + (float("0." + m[4]) if m[4] else 0.0)


def parse_duration(s) -> float:
    s = ("" if s is None else str(s)).strip().replace(",", "")
    if not s:
        return math.nan
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", s):
        return float(s)
    m = re.fullmatch(r"([0-9]+):([0-9]{1,2}):([0-9]{1,2})(?:[.,]([0-9]+))?", s)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + (float("0." + m[4]) if m[4] else 0.0)
    m = re.fullmatch(r"([0-9]+):([0-9]{1,2})(?:[.,]([0-9]+))?", s)
    if m:
        return int(m[1]) * 60 + int(m[2]) + (float("0." + m[3]) if m[3] else 0.0)
    m = re.fullmatch(r"(?:([0-9]+)\s*(?:h|hr|時間))?\s*(?:([0-9]+)\s*(?:m|min|分))?\s*(?:([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|秒))?", s, re.I)
    if m and (m[1] or m[2] or m[3]):
        return int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0)
    return math.nan


def isfinite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def looks_header(row) -> bool:
    if not row:
        return False
    if any(_match_dt(c) for c in row):
        return False
    non_empty = [c for c in row if str(c).strip() != ""]
    if not non_empty:
        return False
    return any(not re.fullmatch(r"[0-9.:\-\s]+", str(c).strip()) for c in non_empty)


@dataclass
class Parsed:
    delim: str
    header: list[str]
    has_header: bool
    data: list[list[str]]
    lead: int


def parse_log(text: str, delim: str | None = None) -> Parsed:
    d = delim or detect_delimiter(text)
    rows = split_rows(text, d)
    freq: dict[int, int] = {}
    for r in rows[:300]:
        freq[len(r)] = freq.get(len(r), 0) + 1
    mode, mf = 0, 0
    for ln, f in freq.items():
        if f > mf or (f == mf and ln > mode):
            mf, mode = f, ln
    lead = 0
    while lead < len(rows) and len(rows[lead]) < max(2, mode * 0.6):
        lead += 1
    rows = rows[lead:]
    has_header = looks_header(rows[0]) if rows else False
    data = rows[1:] if has_header else rows
    cols = len(rows[0]) if has_header else 0
    for r in data[:300]:
        cols = max(cols, len(r))
    header = []
    for k in range(cols):
        h = str(rows[0][k]).strip() if has_header and k < len(rows[0]) else ""
        header.append(h or f"列{k + 1}")
    return Parsed(d, header, has_header, data, lead)


KEYS = {
    "ready": re.compile(r"予定|起動可能|ready|queue|キュー|待ち開始|scheduled|投入", re.I),
    "status": re.compile(r"状態|status|結果|終了コード|戻り値|return|exit|code|state", re.I),
    "group": re.compile(r"ジョブネット|jobnet|ネット名|グループ|group|親|parent|パス|path", re.I),
    "elapsed": re.compile(r"経過|elapsed|所要|実行時間|duration|処理時間|\(秒\)|（秒）|^秒$|sec", re.I),
    "start": re.compile(r"開始|start|begin|起動日時|起動時刻", re.I),
    "end": re.compile(r"^終了$|終了日|終了時|(?:^|[^a-z])end|end_?(?:time|date)|finish|完了|stop", re.I),
    "name": re.compile(r"ジョブ名|ユニット名|job|unit|名称|name|ジョブ|ユニット|タスク|task", re.I),
    "time": re.compile(r"時刻|time", re.I),
}
FIELDS = ["name", "group", "start", "startTime", "end", "endTime", "elapsed", "status", "ready"]


def _ratio(data, col, pred) -> float:
    n = ok = 0
    for r in data:
        if n >= 200:
            break
        v = r[col] if col < len(r) else None
        if v is None or str(v).strip() == "":
            continue
        n += 1
        if pred(v):
            ok += 1
    return ok / n if n else 0.0


def guess_mapping(header: list[str], data: list[list[str]]) -> dict[str, int]:
    used: set[int] = set()
    m = {k: -1 for k in FIELDS}
    is_dt = lambda v: bool(_match_dt(v))  # noqa: E731
    is_tod = lambda v: isfinite(parse_time_of_day(v))  # noqa: E731
    is_dur = lambda v: isfinite(parse_duration(v))  # noqa: E731

    def pick(fld, rx, pred=None) -> bool:
        for k, h in enumerate(header):
            if k in used or not rx.search(h):
                continue
            if pred and _ratio(data, k, pred) < 0.6:
                continue
            m[fld] = k
            used.add(k)
            return True
        return False

    pick("ready", KEYS["ready"], is_dt)
    pick("status", KEYS["status"], lambda v: not is_dt(v))
    pick("group", KEYS["group"], lambda v: not is_dt(v))
    pick("elapsed", KEYS["elapsed"], is_dur)
    if not pick("start", KEYS["start"], is_dt):
        if pick("start", KEYS["start"], is_tod):
            m["startTime"], m["start"] = m["start"], -1
    if not pick("end", KEYS["end"], is_dt):
        pick("endTime", KEYS["end"], is_tod)
    if m["start"] < 0 and m["startTime"] < 0:
        for k in range(len(header)):
            if k not in used and _ratio(data, k, is_dt) >= 0.9:
                m["start"] = k
                used.add(k)
                break
    if m["end"] < 0 and m["endTime"] < 0:
        for k in range(len(header)):
            if k not in used and _ratio(data, k, is_dt) >= 0.9:
                m["end"] = k
                used.add(k)
                break

    def split_time(date_f, time_f, rx):
        if m[date_f] < 0 or m[time_f] >= 0:
            return
        if _ratio(data, m[date_f], has_time_part) >= 0.5:
            return
        for k, h in enumerate(header):
            if k in used:
                continue
            if (rx.search(h) or KEYS["time"].search(h)) and _ratio(data, k, is_tod) >= 0.6:
                m[time_f] = k
                used.add(k)
                return

    split_time("start", "startTime", KEYS["start"])
    split_time("end", "endTime", KEYS["end"])
    if m["startTime"] >= 0 and m["start"] < 0:
        for k, h in enumerate(header):
            if k not in used and re.search(r"日付|date|日$", h, re.I) and _ratio(data, k, is_dt) >= 0.6:
                m["start"] = k
                used.add(k)
                break
    pick("name", KEYS["name"], lambda v: not is_dt(v) and not re.fullmatch(r"[0-9]+", str(v).strip()))
    if m["name"] < 0:
        for k in range(len(header)):
            if k not in used and _ratio(data, k, lambda v: not is_dt(v) and not is_dur(v)) >= 0.8:
                m["name"] = k
                used.add(k)
                break
    return m


@dataclass
class Job:
    id: int
    line: int
    name: str
    group: str
    status: str
    s: float
    e: float
    el: float
    ready: float = math.nan


def _combine(date_str: str, time_str: str, fallback_day: float) -> float:
    if time_str:
        tod = parse_time_of_day(time_str)
        if not isfinite(tod):
            return math.nan
        base = parse_datetime(date_str) if date_str else fallback_day
        if not isfinite(base):
            return math.nan
        return math.floor(base / DAY) * DAY + tod
    return parse_datetime(date_str)


def build_jobs(data, mp, line_offset=0):
    jobs: list[Job] = []
    skipped: list[tuple[int, str]] = []
    mismatch = 0

    def get(r, k):
        c = mp.get(k, -1)
        return str(r[c]).strip() if c is not None and c >= 0 and c < len(r) and r[c] is not None else ""

    for idx, r in enumerate(data):
        line = idx + 1 + line_offset
        s_raw, st_raw = get(r, "start"), get(r, "startTime")
        if not s_raw and not st_raw:
            skipped.append((line, "開始日時が空"))
            continue
        s = _combine(s_raw, st_raw, math.nan)
        if not isfinite(s):
            skipped.append((line, "開始日時を解釈できない"))
            continue
        e_raw, et_raw, el_raw = get(r, "end"), get(r, "endTime"), get(r, "elapsed")
        el = parse_duration(el_raw)
        if e_raw or et_raw:
            e = _combine(e_raw, et_raw, s)
            if not isfinite(e):
                skipped.append((line, "終了日時を解釈できない"))
                continue
            if not e_raw and et_raw and e < s:
                e += DAY
        elif isfinite(el):
            e = s + el
        else:
            skipped.append((line, "終了日時・Elapsed とも空（実行中・未終了）"))
            continue
        if e < s:
            skipped.append((line, "終了が開始より前"))
            continue
        if isfinite(el) and (e_raw or et_raw) and abs((e - s) - el) > max(2, el * 0.01):
            mismatch += 1
        ready = math.nan
        r_raw = get(r, "ready")
        if r_raw:
            rv = parse_datetime(r_raw)
            if isfinite(rv) and rv <= s:
                ready = rv
        jobs.append(Job(len(jobs), line, get(r, "name") or "(名称なし)", get(r, "group"), get(r, "status"), s, e, e - s, ready))
    return jobs, skipped, mismatch


# ---------------------------------------------------------------- 計算


@dataclass
class Series:
    t: list[float] = field(default_factory=list)
    run: list[int] = field(default_factory=list)
    long: list[int] = field(default_factory=list)
    wait: list[int] = field(default_factory=list)


def sweep(jobs: list[Job], is_long: list[bool] | None) -> Series:
    m: dict[float, list[int]] = {}

    def add(t, a, b, c):
        d = m.setdefault(t, [0, 0, 0])
        d[0] += a
        d[1] += b
        d[2] += c

    for i, j in enumerate(jobs):
        if j.e > j.s:
            L = 1 if is_long and is_long[i] else 0
            add(j.s, 1, L, 0)
            add(j.e, -1, -L, 0)
        if isfinite(j.ready) and j.ready < j.s:
            add(j.ready, 0, 0, 1)
            add(j.s, 0, 0, -1)
    S = Series()
    r = l = w = 0
    for t in sorted(m):
        d = m[t]
        r, l, w = r + d[0], l + d[1], w + d[2]
        S.t.append(t)
        S.run.append(r)
        S.long.append(l)
        S.wait.append(w)
    return S


def value_at(S: Series, x: float, key: str = "run") -> int:
    k = bisect.bisect_right(S.t, x) - 1
    return 0 if k < 0 else getattr(S, key)[k]


def value_before(S: Series, x: float, key: str = "run") -> int:
    k = bisect.bisect_left(S.t, x) - 1
    return 0 if k < 0 else getattr(S, key)[k]


def for_segments(S: Series, a: float, b: float):
    """[a, b) を S の階段の切れ目で区切って (s0, s1, k) を返す。k=-1 は最初のイベントより前（0 本）。"""
    n = len(S.t)
    k = bisect.bisect_right(S.t, a) - 1
    if k < 0:
        if n and S.t[0] > a:
            yield a, min(S.t[0], b), -1
        k = 0
    while k < n:
        if S.t[k] >= b:
            break
        s0 = max(S.t[k], a)
        s1 = min(S.t[k + 1] if k + 1 < n else b, b)
        if s1 > s0:
            yield s0, s1, k
        k += 1


def range_stats(S: Series, a: float, b: float, limit: int = 0) -> dict:
    peak, peak_dur, peak_first, sat, area, long_area, max_wait = 0, 0.0, math.nan, 0.0, 0.0, 0.0, 0
    for s0, s1, k in for_segments(S, a, b):
        v = 0 if k < 0 else S.run[k]
        d = s1 - s0
        area += v * d
        if k >= 0:
            long_area += S.long[k] * d
            max_wait = max(max_wait, S.wait[k])
        if v > peak:
            peak, peak_dur, peak_first = v, d, s0
        elif v == peak and v > 0:
            peak_dur += d
        if limit > 0 and v >= limit:
            sat += d
    return dict(peak=peak, peak_dur=peak_dur, peak_first=peak_first, sat=sat,
                avg=area / (b - a) if b > a else 0.0, area=area, long_area=long_area, max_wait=max_wait)


def peak_intervals(S: Series, a: float, b: float, thr: int, merge_gap: float = 60) -> list[dict]:
    out: list[dict] = []
    for s0, s1, k in for_segments(S, a, b):
        v = 0 if k < 0 else S.run[k]
        if v < thr:
            continue
        if out and (s0 <= out[-1]["e"] or s0 - out[-1]["e"] < merge_gap):
            last = out[-1]
            last["e"] = s1
            if v > last["max"]:
                last.update(max=v, maxAt=s0, maxDur=s1 - s0)
            elif v == last["max"]:
                last["maxDur"] += s1 - s0
        else:
            out.append(dict(s=s0, e=s1, max=v, maxAt=s0, maxDur=s1 - s0))
    return out


def long_threshold(jobs: list[Job], mode: str, val: float) -> float:
    if mode == "pct":
        els = sorted((j.el for j in jobs), reverse=True)
        if not els:
            return math.inf
        k = min(len(els), max(1, math.ceil(len(els) * val / 100))) - 1
        return els[k]
    return val * 60


def assign_lanes(jobs: list[Job]) -> tuple[list[int], int]:
    order = sorted(range(len(jobs)), key=lambda i: (jobs[i].s, -jobs[i].e, i))
    lane = [0] * len(jobs)
    active: list[tuple[float, int]] = []
    free: list[int] = []
    count = 0
    for i in order:
        j = jobs[i]
        while active and active[0][0] <= j.s:
            heapq.heappush(free, heapq.heappop(active)[1])
        if not j.e > j.s:
            lane[i] = free[0] if free else count
            continue
        if free:
            L = heapq.heappop(free)
        else:
            L, count = count, count + 1
        lane[i] = L
        heapq.heappush(active, (j.e, L))
    return lane, count


def wait_suspects(jobs: list[Job], S: Series, limit: int) -> list[bool]:
    out = [False] * len(jobs)
    if not limit or limit <= 0:
        return out
    ends: dict[int, int] = {}
    for j in jobs:
        if j.e > j.s:
            k = _jsround(j.e)
            ends[k] = ends.get(k, 0) + 1
    for i, j in enumerate(jobs):
        if j.e > j.s and value_before(S, j.s) >= limit and ends.get(_jsround(j.s), 0) > 0:
            out[i] = True
    return out


def _jsround(x: float) -> int:
    """JavaScript の Math.round と同じ（.5 は +∞ 方向）。"""
    return math.floor(x + 0.5)


def business_day_start(t: float, boundary_hour: int) -> float:
    b = boundary_hour * 3600
    return math.floor((t - b) / DAY) * DAY + b


def list_days(jobs: list[Job], boundary_hour: int) -> list[dict]:
    m: dict[float, dict] = {}
    for j in jobs:
        d = business_day_start(j.s, boundary_hour)
        x = m.setdefault(d, dict(start=d, count=0, first=math.inf, last=-math.inf))
        x["count"] += 1
        x["first"] = min(x["first"], j.s)
        x["last"] = max(x["last"], j.e)
    return [m[k] for k in sorted(m)]


def hourly_matrix(S: Series, days: list[dict]) -> list[list[dict]]:
    out = []
    for d in days:
        row = []
        for h in range(24):
            a = d["start"] + h * 3600
            st = range_stats(S, a, a + 3600, 0)
            row.append(dict(a=a, peak=st["peak"], avg=st["avg"]))
        out.append(row)
    return out


# ---------------------------------------------------------------- 分析の一式


@dataclass
class Options:
    encoding: str = "auto"
    delimiter: str | None = None
    mapping: dict[str, int] | None = None
    limit: int = 0
    long_mode: str = "min"
    long_val: float = 20
    boundary: int = 12
    day: str = "all"
    filter: str = ""


def _filter_jobs(jobs: list[Job], expr: str) -> list[Job]:
    toks = [t.strip().lower() for t in re.split(r"[,、\s]+", expr or "") if t.strip()]
    inc = [t for t in toks if not t.startswith("!")]
    exc = [t[1:] for t in toks if t.startswith("!") and len(t) > 1]
    out = []
    for j in jobs:
        hay = (j.name + " " + j.group).lower()
        if inc and not any(t in hay for t in inc):
            continue
        if any(t in hay for t in exc):
            continue
        out.append(j)
    return out


def analyze(raw: bytes, opt: Options) -> dict:
    text, enc = decode_bytes(raw, opt.encoding)
    p = parse_log(text, opt.delimiter)
    mp = guess_mapping(p.header, p.data)
    if opt.mapping:
        mp.update(opt.mapping)
    all_jobs, skipped, mismatch = build_jobs(p.data, mp, p.lead + (1 if p.has_header else 0))
    jobs = _filter_jobs(all_jobs, opt.filter)
    for i, j in enumerate(jobs):
        j.id = i
    days = list_days(jobs, opt.boundary)
    if not jobs:
        sel = (0.0, 3600.0)
    elif opt.day == "all":
        a = min(min(j.s for j in jobs), min((j.ready for j in jobs if isfinite(j.ready)), default=math.inf))
        sel = (a, max(j.e for j in jobs))
    else:
        want = parse_datetime(opt.day.replace("-", "/") + " 00:00:00")
        d = next((x for x in days if math.floor(x["start"] / DAY) * DAY == want), None)
        if d is None:
            raise SystemExit(f"業務日 {opt.day} のジョブがありません。対象: " + ", ".join(_fmt_day(x["start"]) for x in days))
        sel = (d["first"], d["last"])
    if sel[1] - sel[0] < 60:
        sel = (sel[0], sel[0] + 60)
    in_sel = [j for j in jobs if j.e > sel[0] and j.s < sel[1]]
    thr = long_threshold(in_sel or jobs, opt.long_mode, opt.long_val)
    long_flag = [j.el >= thr for j in jobs]
    S = sweep(jobs, long_flag)
    lanes, lane_count = assign_lanes(jobs)
    sus = wait_suspects(jobs, S, opt.limit)
    start_conc = [value_before(S, j.s) for j in jobs]
    st = range_stats(S, sel[0], sel[1], opt.limit)
    peak_thr = opt.limit or max(1, math.ceil(st["peak"] * 0.8))
    peaks = peak_intervals(S, sel[0], sel[1], peak_thr, 60)
    return dict(encoding=enc, parsed=p, mapping=mp, all_jobs=all_jobs, jobs=jobs, skipped=skipped, mismatch=mismatch,
                days=days, sel=sel, long_thr=thr, long_flag=long_flag, S=S, lanes=lanes, lane_count=lane_count,
                suspect=sus, start_conc=start_conc, stats=st, peak_thr=peak_thr, peaks=peaks,
                hourly=hourly_matrix(S, days), opt=opt)


def summary_json(r: dict) -> dict:
    """JS 実装との突合用。jp1-telemetry.html と同じ値になるはずの項目だけ。"""
    S: Series = r["S"]
    return dict(
        mapping=r["mapping"], n_jobs=len(r["jobs"]), skipped=[list(x) for x in r["skipped"]], mismatch=r["mismatch"],
        sel=list(r["sel"]), long_thr=r["long_thr"], long_count=sum(r["long_flag"]),
        stats={k: r["stats"][k] for k in ("peak", "peak_dur", "peak_first", "sat", "avg")},
        lane_count=r["lane_count"], lanes=r["lanes"], suspects=[i for i, x in enumerate(r["suspect"]) if x],
        start_conc=r["start_conc"], peak_thr=r["peak_thr"],
        peaks=[[p["s"], p["e"], p["max"], p["maxAt"], p["maxDur"]] for p in r["peaks"]],
        series=[[S.t[k], S.run[k], S.long[k], S.wait[k]] for k in range(len(S.t))],
        days=[[d["start"], d["count"], d["first"], d["last"]] for d in r["days"]],
        hourly=[[c["peak"] for c in row] for row in r["hourly"]],
    )


# ---------------------------------------------------------------- 書き出し


def _dt(t: float) -> str:
    import datetime as _d
    return (_d.datetime(1970, 1, 1) + _d.timedelta(seconds=round(t))).strftime("%Y/%m/%d %H:%M:%S")


def _md_hm(t: float) -> str:
    return _dt(t)[5:16]


def _fmt_day(t: float) -> str:
    import datetime as _d
    d = _d.datetime(1970, 1, 1) + _d.timedelta(seconds=round(t))
    return f"{d:%m/%d}({WD[d.weekday()]})"


def fmt_dur(sec: float) -> str:
    sec = round(sec)
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec / 60:.1f}分" if sec < 600 else f"{sec / 60:.0f}分"
    tm = round(sec / 60)
    h, m = divmod(tm, 60)
    return f"{h}時間" + (f"{m}分" if m else "")


def _num(x: float):
    """整数なら整数で書く（120.0 ではなく 120）。"""
    x = round(x, 3)
    return int(x) if float(x).is_integer() else x


def _write_csv(path: Path, rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, lineterminator="\r\n").writerows(rows)


def write_outputs(r: dict, out: Path, src_name: str, png: bool) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    J: list[Job] = r["jobs"]
    S: Series = r["S"]
    st, sel, lim = r["stats"], r["sel"], r["opt"].limit
    files = []

    rows = [["開始", "終了", "秒", "実行中", "うち長時間", "待ち"]]
    for s0, s1, k in for_segments(S, sel[0], sel[1]):
        rows.append([_dt(s0), _dt(s1), _num(s1 - s0), 0 if k < 0 else S.run[k], 0 if k < 0 else S.long[k], 0 if k < 0 else S.wait[k]])
    _write_csv(out / "timeline.csv", rows)
    files.append(out / "timeline.csv")

    rows = [["ジョブネット", "ジョブ名", "開始", "終了", "Elapsed秒", "長時間", "開始直前の多重度", "スロット", "枠待ちの疑い", "待ち秒", "状態", "元データ行"]]
    for i, j in enumerate(J):
        if j.e > sel[0] and j.s < sel[1]:
            rows.append([j.group, j.name, _dt(j.s), _dt(j.e), _num(j.el), int(r["long_flag"][i]), r["start_conc"][i],
                         r["lanes"][i] + 1, int(r["suspect"][i]), _num(j.s - j.ready) if isfinite(j.ready) else "", j.status, j.line])
    _write_csv(out / "jobs.csv", rows)
    files.append(out / "jobs.csv")

    rows = [["開始", "終了", "継続秒", "最大多重度", "最大到達", "判定ライン"]]
    for p in r["peaks"]:
        rows.append([_dt(p["s"]), _dt(p["e"]), _num(p["e"] - p["s"]), p["max"], _dt(p["maxAt"]), r["peak_thr"]])
    _write_csv(out / "peaks.csv", rows)
    files.append(out / "peaks.csv")

    bh = r["opt"].boundary
    rows = [["業務日"] + [f"{(h + bh) % 24:02d}時" for h in range(24)]]
    for d, row in zip(r["days"], r["hourly"]):
        rows.append([_fmt_day(d["start"])] + [c["peak"] for c in row])
    _write_csv(out / "hourly.csv", rows)
    files.append(out / "hourly.csv")

    # ---- report.md
    in_sel = [i for i, j in enumerate(J) if j.e > sel[0] and j.s < sel[1]]
    long_n = sum(1 for i in in_sel if r["long_flag"][i])
    sus_n = sum(1 for i in in_sel if r["suspect"][i] and sel[0] <= J[i].s < sel[1])
    share = round(100 * st["long_area"] / st["area"]) if st["area"] > 0 else 0
    thr_txt = fmt_dur(r["long_thr"]) if isfinite(r["long_thr"]) else "—"
    L = [f"# 多重度ピーク分析：{src_name}", ""]
    L += [f"- 対象：{_dt(sel[0])} 〜 {_dt(sel[1])}（{fmt_dur(sel[1] - sel[0])}）・業務日 {r['opt'].day}・日の切替 {bh} 時",
          f"- 読込 {len(r['all_jobs'])} 件 / 除外 {len(r['skipped'])} 件" + (f"（例：{r['skipped'][0][0]} 行目 {r['skipped'][0][1]}）" if r["skipped"] else "")
          + (f"・絞り込み「{r['opt'].filter}」後 {len(J)} 件" if r["opt"].filter else ""),
          f"- 文字コード {r['encoding']}・列の割り当て " + "、".join(f"{k}={r['parsed'].header[v]}" for k, v in r["mapping"].items() if v >= 0), ""]
    L += ["| 指標 | 値 |", "|---|---|",
          f"| ピーク多重度 | {st['peak']} 本（{_dt(st['peak_first']) if isfinite(st['peak_first']) else '—'} から、計 {fmt_dur(st['peak_dur'])}） |",
          f"| 平均多重度 | {st['avg']:.1f} 本" + (f"（上限 {lim} 本の {round(100 * st['avg'] / lim)}%）" if lim else "") + " |",
          f"| 上限張り付き | " + (f"{fmt_dur(st['sat'])}（多重度 {lim} 本以上の合計）" if lim else "—（--limit 未指定）") + " |",
          f"| 長時間ジョブ | {long_n} 本（しきい値 {thr_txt}・枠占有 {share}%） |",
          f"| 枠待ちの疑い | " + (f"{sus_n} 本（上限到達中に別ジョブ終了と同時刻に開始）" if lim else "—（--limit 未指定）") + " |", ""]
    L += [f"## ピーク区間（多重度 {r['peak_thr']} 本以上）", "", "| 開始 | 終了 | 継続 | 最大 | 区間中の長時間ジョブ | 開始数 |", "|---|---|---:|---:|---|---:|"]
    top = sorted(sorted(r["peaks"], key=lambda p: (-p["max"], -(p["e"] - p["s"])))[:15], key=lambda p: p["s"])
    for p in top:
        act = [i for i, j in enumerate(J) if j.s < p["e"] and j.e > p["s"]]
        lg = sorted((i for i in act if r["long_flag"][i]), key=lambda i: -J[i].el)
        names = "、".join(J[i].name for i in lg[:3]) + (f" ほか{len(lg) - 3}本" if len(lg) > 3 else "")
        starts = sum(1 for j in J if p["s"] <= j.s < p["e"])
        L.append(f"| {_md_hm(p['s'])} | {_md_hm(p['e'])} | {fmt_dur(p['e'] - p['s'])} | {p['max']} | {names or '—'} | {starts} |")
    if not top:
        L.append("| — | | | | | |")
    L += ["", "## 長時間ジョブ（Elapsed 上位 20）", "", "| ジョブ名 | ジョブネット | 開始 | Elapsed | 開始直前の多重度 | 枠待ちの疑い |", "|---|---|---|---:|---:|---|"]
    for i in sorted(in_sel, key=lambda i: -J[i].el)[:20]:
        j = J[i]
        mark = "**" if r["long_flag"][i] else ""
        L.append(f"| {mark}{j.name}{mark} | {j.group} | {_md_hm(j.s)} | {fmt_dur(j.el)} | {r['start_conc'][i]}"
                 + (f" / {lim}" if lim else "") + f" | {'あり' if r['suspect'][i] else ''} |")
    L += ["", "## 業務日 × 時刻の最大多重度", "", "| 業務日 | " + " | ".join(f"{(h + bh) % 24:02d}" for h in range(24)) + " |",
          "|---|" + "---:|" * 24]
    for d, row in zip(r["days"], r["hourly"]):
        L.append(f"| {_fmt_day(d['start'])} | " + " | ".join((f"**{c['peak']}**" if lim and c["peak"] >= lim else str(c["peak"])) for c in row) + " |")
    L += ["", "太字は多重度上限以上。定義は jp1-telemetry.html の「指標の定義と読み方」と同じ。", ""]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    files.insert(0, out / "report.md")

    if png:
        p = _plot_png(r, out / "concurrency.png", src_name)
        if p:
            files.append(p)
    return files


def _plot_png(r: dict, path: Path, title: str) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import datetime as _d
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.ticker import FuncFormatter, MaxNLocator
    except ImportError:
        print("matplotlib が無いため PNG は出力しません（CSV とレポートは出力済み）", file=sys.stderr)
        return None
    for fam in ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", "IPAexGothic", "Hiragino Sans"]:
        if any(fam in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = fam
            break
    J, S, sel, lim = r["jobs"], r["S"], r["sel"], r["opt"].limit
    RED, BLUE, ORG = "#e34948", "#2a78d6", "#eb6834"
    hrs = lambda t: mdates.date2num(_d.datetime(1970, 1, 1) + _d.timedelta(seconds=t))  # noqa: E731
    fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw=dict(height_ratios=[1.1, 1, 1.4]))
    xs, run, lng = [], [], []
    for s0, s1, k in for_segments(S, sel[0], sel[1]):
        xs += [hrs(s0), hrs(s1)]
        v = 0 if k < 0 else S.run[k]
        lv = 0 if k < 0 else S.long[k]
        run += [v, v]
        lng += [lv, lv]
    ax[0].fill_between(xs, 0, lng, color=RED, alpha=0.6, linewidth=0, label="長時間ジョブ")
    ax[0].fill_between(xs, lng, run, color=BLUE, alpha=0.22, linewidth=0, label="その他")
    ax[0].plot(xs, run, color=BLUE, lw=1.2)
    for p in r["peaks"]:
        ax[0].axvspan(hrs(p["s"]), hrs(p["e"]), color=RED, alpha=0.07, lw=0)
    if lim:
        ax[0].axhline(lim, ls="--", color="#52514e", lw=1)
    ax[0].set_ylabel("同時実行（本）")
    ax[0].legend(loc="upper right", frameon=False)
    in_sel = [i for i, j in enumerate(J) if j.e > sel[0] and j.s < sel[1]]
    for i in sorted(in_sel, key=lambda i: r["long_flag"][i]):
        j, lg = J[i], r["long_flag"][i]
        y = j.el / 60
        ax[1].plot([hrs(j.s), hrs(j.e)], [y, y], color=RED if lg else BLUE, alpha=0.85 if lg else 0.3, lw=2 if lg else 1)
        ax[1].plot(hrs(j.s), y, "o", color=RED if lg else BLUE, ms=5 if lg else 3)
    if isfinite(r["long_thr"]):
        ax[1].axhline(r["long_thr"] / 60, ls="--", color=RED, lw=1)
    ax[1].set_ylabel("Elapsed（分）")
    for i in in_sel:
        j = J[i]
        ax[2].barh(r["lanes"][i], hrs(j.e) - hrs(j.s), left=hrs(j.s), height=0.8,
                   color=RED if r["long_flag"][i] else BLUE, alpha=1 if r["long_flag"][i] else 0.55)
    ax[2].invert_yaxis()
    ax[2].yaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
    ax[2].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v) + 1}"))
    ax[2].set_ylabel("スロット")
    span_h = (sel[1] - sel[0]) / 3600
    if span_h <= 6:
        loc = mdates.MinuteLocator(byminute=[0, 30])
    elif span_h <= 48:
        loc = mdates.HourLocator(interval=next(n for n in (1, 2, 3, 6, 12) if span_h / n <= 10))
    else:
        loc = mdates.DayLocator()
    ax[2].xaxis.set_major_locator(loc)
    ax[2].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%m/%d" if span_h <= 48 else "%m/%d"))
    for a in ax:
        a.grid(axis="y", color="#e1e0d9", lw=0.8)
        a.spines[["top", "right"]].set_visible(False)
    fig.suptitle(f"多重度ピーク分析：{title}", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- CLI


def _parse_map(expr: str, header: list[str]) -> dict[str, int]:
    out = {}
    for part in filter(None, (x.strip() for x in expr.split(","))):
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in FIELDS:
            raise SystemExit(f"--map の項目名 {k!r} は不明です。使える項目: {', '.join(FIELDS)}")
        v = v.strip()
        if v in ("", "-", "none"):
            out[k] = -1
        elif v in header:
            out[k] = header.index(v)
        elif v.isdigit() and 1 <= int(v) <= len(header):
            out[k] = int(v) - 1
        else:
            raise SystemExit(f"--map の列 {v!r} が見つかりません。列: {', '.join(header)}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JP1 実行履歴から多重度ピークを分析する（標準ライブラリのみ）")
    ap.add_argument("log", type=Path, help="実行履歴 CSV / TSV")
    ap.add_argument("--encoding", default="auto", help="auto（既定）/ utf-8 / shift_jis")
    ap.add_argument("--delimiter", default="auto", help="auto（既定）/ , / tab / ; / | / ws（2文字以上の空白揃え）")
    ap.add_argument("--map", default="", help="列の割り当て。例: start=開始日時,end=終了日時,name=ジョブ名（列番号 1〜 でも可）")
    ap.add_argument("--limit", type=int, default=0, help="多重度上限（張り付き時間・枠待ちの疑いの算出に使う）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--long-min", type=float, help="長時間ジョブのしきい値（分）。既定 20")
    g.add_argument("--long-pct", type=float, help="長時間ジョブを Elapsed 上位 N%% で判定")
    ap.add_argument("--boundary", type=int, default=12, help="業務日の切替時刻（時）。既定 12")
    ap.add_argument("--day", default="all", help="業務日 YYYY-MM-DD（既定 all＝全期間）")
    ap.add_argument("--filter", default="", help="ジョブ名・ジョブネットの部分一致。カンマ区切り、!で除外")
    ap.add_argument("--out", type=Path, help="出力フォルダ（既定 ./jp1-report-<入力名>）")
    ap.add_argument("--png", action="store_true", help="matplotlib があれば concurrency.png も出力")
    ap.add_argument("--json", action="store_true", help="突合用の要約 JSON を標準出力に出す（ファイルは書かない）")
    a = ap.parse_args(argv)

    raw = a.log.read_bytes()
    delim = {"auto": None, "tab": "\t", "\\t": "\t"}.get(a.delimiter, a.delimiter)
    opt = Options(encoding=a.encoding, delimiter=delim, limit=max(0, a.limit), boundary=max(0, min(23, a.boundary)),
                  day=a.day, filter=a.filter)
    if a.long_pct is not None:
        opt.long_mode, opt.long_val = "pct", a.long_pct
    elif a.long_min is not None:
        opt.long_val = a.long_min
    if a.map:
        text, _ = decode_bytes(raw, opt.encoding)
        opt.mapping = _parse_map(a.map, parse_log(text, delim).header)
    r = analyze(raw, opt)
    mp = r["mapping"]
    if (mp["start"] < 0 and mp["startTime"] < 0) or (mp["end"] < 0 and mp["endTime"] < 0 and mp["elapsed"] < 0):
        print("開始日時と、終了日時または Elapsed の列を判別できませんでした。--map で指定してください。列: "
              + ", ".join(r["parsed"].header), file=sys.stderr)
        return 2
    if a.json:
        json.dump(summary_json(r), sys.stdout, ensure_ascii=False, default=lambda x: None if isinstance(x, float) and not math.isfinite(x) else x)
        return 0
    out = a.out or Path(f"jp1-report-{a.log.stem}")
    files = write_outputs(r, out, a.log.name, a.png)
    st = r["stats"]
    print(f"読込 {len(r['all_jobs'])} 件（除外 {len(r['skipped'])}）・ピーク多重度 {st['peak']} 本・平均 {st['avg']:.1f} 本"
          + (f"・上限張り付き {fmt_dur(st['sat'])}" if opt.limit else ""))
    for f in files:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
