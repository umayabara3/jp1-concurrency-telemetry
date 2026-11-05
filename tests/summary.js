// 突合用：jp1-telemetry.html の CORE で CSV を分析し、tools/jp1_concurrency.py --json と同じ形の JSON を出す
//   node tests/summary.js <log> '{"limit":20,"long_mode":"min","long_val":20,"boundary":12,"day":"all","filter":"","encoding":"auto"}'
const fs = require('fs');
const { loadCore } = require('./load_core.js');
const C = loadCore();
const [file, optJson] = process.argv.slice(2);
const o = Object.assign({ limit: 0, long_mode: 'min', long_val: 20, boundary: 12, day: 'all', filter: '', encoding: 'auto' }, JSON.parse(optJson || '{}'));
const { text } = C.decodeBytes(new Uint8Array(fs.readFileSync(file)), o.encoding);
const p = C.parseLog(text, null);
const map = C.guessMapping(p.header, p.data);
const r = C.buildJobs(p.data, map, p.lead + (p.hasHeader ? 1 : 0));
let day = 'all';
if (o.day !== 'all') { const [y, m, d] = o.day.split('-').map(Number); day = Date.UTC(y, m - 1, d) / 1000 + o.boundary * 3600; }
const A = C.analyze(r.jobs, { filter: o.filter, boundary: o.boundary, day, longMode: o.long_mode, longVal: o.long_val, limit: o.limit });
const S = A.S, fin = x => (Number.isFinite(x) ? x : null);
const out = {
  mapping: map, n_jobs: A.jobs.length, skipped: r.skipped.map(s => [s.line, s.reason]), mismatch: r.mismatch,
  sel: A.sel, long_thr: fin(A.longThr), long_count: A.longFlag.filter(Boolean).length,
  stats: { peak: A.stats.peak, peak_dur: A.stats.peakDur, peak_first: fin(A.stats.peakFirst), sat: A.stats.sat, avg: A.stats.avg },
  lane_count: A.lanes.count, lanes: Array.from(A.lanes.lane), suspects: Array.from(A.suspect).map((x, i) => (x ? i : -1)).filter(i => i >= 0),
  start_conc: A.startConc, peak_thr: A.peakThr,
  peaks: A.peaks.map(q => [q.s, q.e, q.max, q.maxAt, q.maxDur]),
  series: Array.from(S.t, (t, k) => [t, S.run[k], S.long[k], S.wait[k]]),
  days: A.days.map(d => [d.start, d.count, d.first, d.last]),
  hourly: C.hourlyMatrix(S, A.days, o.boundary).map(row => row.map(c => c.peak)),
  cap: null,
};
if (o.cap) {
  const c = Object.assign({ heap: 512, nonheap: 256, cpu: 0.5, boot_cpu: 1.5, boot_sec: 15, factor: 1, os_gb: 8, margin: 30, util: 70, cpu_window: 300, profile: null }, o.cap);
  const rules = c.profile ? C.parseProfile(C.decodeBytes(new Uint8Array(fs.readFileSync(c.profile)), 'auto').text).rules : [];
  const K = C.capacity(A, rules, { heap: c.heap, nonheap: c.nonheap, cpu: c.cpu, bootCpu: c.boot_cpu, bootSec: c.boot_sec, factor: c.factor, osGB: c.os_gb, margin: c.margin, util: c.util, cpuWindow: c.cpu_window, limit: o.limit });
  out.cap = {
    rules: K.prof.map(q => q.rule), mem: K.prof.map(q => q.mem), range: K.range,
    mem_peak: K.memPeak, mem_at: fin(K.memAt), cpu_peak: K.cpuPeak, cpu_at: fin(K.cpuAt), cpu_sus: K.cpuSus, cpu_sus_at: fin(K.cpuSusAt),
    run_peak: K.runPeak, max_mem: K.maxMem, guarantee: K.guarantee, at_peak: K.atPeak, mem_rec_gb: K.memRecGB, cores_rec: K.coresRec,
    ume_gb: K.umeGB, guarantee_gb: K.guaranteeGB, series: Array.from(K.C.t, (t, k) => [t, K.C.mem[k], K.C.run[k], K.C.cpu[k]]),
  };
}
process.stdout.write(JSON.stringify(out));
