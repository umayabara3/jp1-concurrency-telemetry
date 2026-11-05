// jp1-telemetry.html の CORE 単体テスト（依存なし）:  node tests/test_core.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { loadCore } = require('./load_core.js');
const C = loadCore();
const T = (y, mo, d, h = 0, mi = 0, s = 0) => Date.UTC(y, mo - 1, d, h, mi, s) / 1000;
const job = (s, e, extra = {}) => Object.assign({ name: 'j', group: '', status: '', s, e, el: e - s, ready: NaN }, extra);
let n = 0;
// vm で読み込んだ配列は別コンテキストのプロトタイプを持つので、JSON 経由で素の値にして比べる
const deepEq = (a, b) => assert.deepStrictEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)));
const test = (name, fn) => { fn(); n++; console.log('ok', name); };

test('日時の書式', () => {
  assert.strictEqual(C.parseDateTime('2026/11/04 22:00:00'), T(2026, 11, 4, 22));
  assert.strictEqual(C.parseDateTime('2026-11-04T22:00:00'), T(2026, 11, 4, 22));
  assert.strictEqual(C.parseDateTime('2026/11/4 2:05'), T(2026, 11, 4, 2, 5));
  assert.strictEqual(C.parseDateTime('20261104220503'), T(2026, 11, 4, 22, 5, 3));
  assert.strictEqual(C.parseDateTime('2026年11月4日 22時05分10秒'), T(2026, 11, 4, 22, 5, 10));
  assert.strictEqual(C.parseDateTime('2026/11/04 22:00:00.250'), T(2026, 11, 4, 22) + 0.25);
  assert.strictEqual(C.parseDateTime('2026-11-04 22:00:00+09:00'), T(2026, 11, 4, 22));
  assert.ok(Number.isNaN(C.parseDateTime('正常終了')));
  assert.ok(Number.isNaN(C.parseDateTime('2026/13/01 00:00')));
  assert.strictEqual(C.parseTimeOfDay('22:05:03'), 22 * 3600 + 5 * 60 + 3);
});

test('Elapsed の書式', () => {
  assert.strictEqual(C.parseDuration('864'), 864);
  assert.strictEqual(C.parseDuration('1,234.5'), 1234.5);
  assert.strictEqual(C.parseDuration('00:14:24'), 864);
  assert.strictEqual(C.parseDuration('14:24'), 864);
  assert.strictEqual(C.parseDuration('1h 2m 3s'), 3723);
  assert.strictEqual(C.parseDuration('1時間2分'), 3720);
  assert.ok(Number.isNaN(C.parseDuration('abc')));
});

test('区切り文字の判別とクォート', () => {
  assert.strictEqual(C.detectDelimiter('a,b,c\n1,2,3\n'), ',');
  assert.strictEqual(C.detectDelimiter('a\tb\tc\n1\t2\t3\n'), '\t');
  assert.strictEqual(C.detectDelimiter('a;b;c\n1;2;3\n'), ';');
  assert.strictEqual(C.detectDelimiter('名前      開始\nx     2026/11/04 22:00:00\n'), 'ws');
  const rows = C.splitRows('"a,b","c""d",e\r\n1,2,3\r\n', ',');
  deepEq(rows, [['a,b', 'c"d', 'e'], ['1', '2', '3']]);
  deepEq(C.splitRows('ジョブ名  2026/11/04 22:00:00  864\n', 'ws'), [['ジョブ名', '2026/11/04 22:00:00', '864']]);
});

test('半開区間：同じ秒の終了と開始は重ねない', () => {
  const S = C.sweep([job(0, 10), job(10, 20), job(10, 10)], null);
  assert.strictEqual(C.rangeStats(S, 0, 20, 0).peak, 1);
  assert.strictEqual(C.valueBefore(S, 10), 1);
  assert.strictEqual(C.valueAt(S, 10), 1);
  assert.strictEqual(C.valueAt(S, 20), 0);
});

test('多重度・張り付き・平均', () => {
  const J = [job(0, 100), job(0, 50), job(25, 75)];
  const S = C.sweep(J, [true, false, false]);
  const r = C.rangeStats(S, 0, 100, 2);
  assert.strictEqual(r.peak, 3);
  assert.strictEqual(r.peakFirst, 25);
  assert.strictEqual(r.peakDur, 25);
  assert.strictEqual(r.sat, 75);              // 0-25:2, 25-50:3, 50-75:2, 75-100:1
  assert.strictEqual(r.avg, (2 * 25 + 3 * 25 + 2 * 25 + 1 * 25) / 100);
  assert.strictEqual(r.longArea, 100);
  deepEq(C.peakIntervals(S, 0, 100, 2, 0).map(p => [p.s, p.e, p.max]), [[0, 75, 3]]);
});

test('ピーク区間は 60 秒未満の切れ目を連結', () => {
  const S = C.sweep([job(0, 100), job(0, 100), job(130, 200), job(130, 200), job(300, 400), job(300, 400)], null);
  deepEq(C.peakIntervals(S, 0, 400, 2, 60).map(p => [p.s, p.e]), [[0, 200], [300, 400]]);
});

test('スロット詰めの行数＝ピーク多重度、同じ行は重ならない', () => {
  const rnd = (() => { let x = 7; return () => (x = (x * 48271) % 2147483647) / 2147483647; })();
  const J = Array.from({ length: 400 }, () => { const s = Math.floor(rnd() * 5000); return job(s, s + 1 + Math.floor(rnd() * 900)); });
  const L = C.assignLanes(J), S = C.sweep(J, null);
  assert.strictEqual(L.count, C.rangeStats(S, 0, 7000, 0).peak);
  const by = new Map(); J.forEach((j, i) => { const k = L.lane[i]; if (!by.has(k)) by.set(k, []); by.get(k).push(j); });
  by.forEach(a => { a.sort((x, y) => x.s - y.s); for (let i = 1; i < a.length; i++) assert.ok(a[i].s >= a[i - 1].e); });
});

test('枠待ちの疑い：上限到達中に別ジョブ終了と同時刻に開始', () => {
  const J = [job(0, 100), job(0, 50), job(50, 120), job(60, 70)];
  const S = C.sweep(J, null);
  deepEq(Array.from(C.waitSuspects(J, S, 2)), [0, 0, 1, 0]);
  deepEq(Array.from(C.waitSuspects(J, S, 0)), [0, 0, 0, 0]);
});

test('長時間しきい値：分 と 上位％', () => {
  const J = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100].map(m => job(0, m * 60));
  assert.strictEqual(C.longThreshold(J, 'min', 20), 1200);
  assert.strictEqual(C.longThreshold(J, 'pct', 10), 6000);
  assert.strictEqual(C.longThreshold(J, 'pct', 25), 4800);
});

test('業務日：切替 12 時なら 22:00 と翌 03:00 は同じ日', () => {
  assert.strictEqual(C.businessDayStart(T(2026, 11, 4, 22), 12), T(2026, 11, 4, 12));
  assert.strictEqual(C.businessDayStart(T(2026, 11, 5, 3), 12), T(2026, 11, 4, 12));
  assert.strictEqual(C.businessDayStart(T(2026, 11, 5, 13), 12), T(2026, 11, 5, 12));
});

test('列の自動判別：サンプルとフィクスチャ', () => {
  const read = f => { const { text } = C.decodeBytes(new Uint8Array(fs.readFileSync(path.join(__dirname, '..', f))), 'auto'); const p = C.parseLog(text); return { p, m: C.guessMapping(p.header, p.data) }; };
  let { p, m } = read('samples/sample_1day_100jobs.csv');
  deepEq([m.name, m.group, m.start, m.end, m.elapsed, m.status], [1, 0, 2, 3, 4, 5]);
  ({ p, m } = read('tests/fixtures/sjis_split_datetime.tsv'));
  assert.strictEqual(p.delim, '\t');
  assert.strictEqual(p.header[0], 'ユニット名');
  deepEq([m.name, m.start, m.startTime, m.end, m.endTime], [0, 1, 2, -1, 3]);
  const r = C.buildJobs(p.data, m, 1);
  assert.strictEqual(r.jobs.length, 100);
  const last = r.jobs[r.jobs.length - 1];
  assert.strictEqual(last.e, T(2026, 11, 5, 1, 34, 8));     // 終了日の列がなくても日付またぎを補正
  ({ p, m } = read('tests/fixtures/elapsed_only.csv'));
  assert.strictEqual(p.lead, 1);
  deepEq([m.name, m.start, m.end, m.elapsed], [0, 1, -1, 2]);
  ({ p, m } = read('tests/fixtures/noheader_quoted.csv'));
  assert.strictEqual(p.hasHeader, false);
  deepEq([m.start, m.end], [2, 3]);
  ({ p, m } = read('tests/fixtures/ws_aligned.txt'));
  assert.strictEqual(p.delim, 'ws');
  deepEq([m.name, m.start, m.end, m.elapsed], [0, 1, 2, 3]);
});

test('除外行は理由つきで数える', () => {
  const p = C.parseLog('ジョブ名,開始日時,終了日時\na,2026/11/04 22:00:00,2026/11/04 22:10:00\nb,2026/11/04 22:00:00,\nc,不明,2026/11/04 22:10:00\nd,2026/11/04 22:10:00,2026/11/04 22:00:00\n');
  const r = C.buildJobs(p.data, C.guessMapping(p.header, p.data), 1);
  assert.strictEqual(r.jobs.length, 1);
  deepEq(r.skipped.map(s => [s.line, s.reason]), [[3, '終了日時・Elapsed とも空（実行中・未終了）'], [4, '開始日時を解釈できない'], [5, '終了が開始より前']]);
});

test('analyze：絞り込み・業務日・待ちの実測', () => {
  const all = [job(T(2026, 11, 4, 22), T(2026, 11, 4, 23), { name: '取込A' }), job(T(2026, 11, 4, 22, 30), T(2026, 11, 5, 1), { name: 'バックアップ', ready: T(2026, 11, 4, 22, 10) }), job(T(2026, 11, 5, 22), T(2026, 11, 5, 23), { name: '取込B' })];
  const A = C.analyze(all, { filter: '', boundary: 12, day: T(2026, 11, 4, 12), longMode: 'min', longVal: 60, limit: 0 });
  deepEq(A.sel, [T(2026, 11, 4, 22), T(2026, 11, 5, 1)]);
  assert.strictEqual(A.days.length, 2);
  assert.strictEqual(C.valueAt(A.S, T(2026, 11, 4, 22, 15), 'wait'), 1);
  deepEq(A.longFlag, [true, true, true]);
  const B = C.analyze(all, { filter: '取込, !B', boundary: 12, day: 'all', longMode: 'min', longVal: 20, limit: 0 });
  deepEq(B.jobs.map(j => j.name), ['取込A']);
});

console.log(`\n${n} tests passed`);
