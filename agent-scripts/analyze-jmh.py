#!/usr/bin/env python3
"""
analyze-jmh.py — Robust post-processing for JMH JSON output.

Reads one or two JMH JSON files (a baseline and an optional post-change run)
and reports:

  - Raw mean / median / 10%-trimmed mean / 20%-trimmed mean
  - StdDev, IQR, 95% CI half-width on each estimator (bootstrap, n=10000)
  - Per-fork breakdown (between-fork SD vs within-fork SD)
  - Suspected-warmup-tail flag if the FIRST iter of each fork differs from
    the within-fork mean by > 1.5x within-fork SD
  - "Drop first" rerun: same statistics, with the first iter of each fork
    discarded

If two files are passed, also reports the delta (post - baseline) in absolute
ms/op and percent, with a 95% bootstrap CI on each estimator's delta. The
script does NOT do paired comparison (the bench runs are independent forks),
just two-sample bootstrap with resampling within each side.

Usage:
  analyze-jmh.py PATH_TO_BASELINE.json
  analyze-jmh.py PATH_TO_BASELINE.json PATH_TO_POSTCHANGE.json
  analyze-jmh.py --drop-first N PATH_TO_BASELINE.json [PATH_TO_POSTCHANGE.json]

Flags:
  --drop-first N   How many iters of EACH fork to drop in the drop-first
                   rerun (default: 1). Iter 20's no-op cross-val showed
                   the iter-2 measurement is still on the warmup tail
                   under 1f x 30i x 10s; for the 3-fork x 12-iter config
                   recommended from iter 21 onwards, pass --drop-first 2.

Exit code 0 always; this is a reporting script, not a gate.
"""
from __future__ import annotations
import argparse, json, statistics, sys, random
from pathlib import Path

random.seed(20260511)

def load(p):
    with open(p) as f:
        return json.load(f)[0]

def percentile(xs, q):
    # q in [0,100]
    s = sorted(xs); n = len(s)
    if n == 0: return float('nan')
    k = (n - 1) * (q / 100.0)
    lo = int(k); hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def trimmed_mean(xs, frac):
    s = sorted(xs); n = len(s)
    k = int(n * frac)
    if k * 2 >= n: return statistics.mean(s)
    return statistics.mean(s[k:n-k])

def bootstrap_ci(xs, stat, B=10000, q=2.5):
    # 95% CI half-width via percentile bootstrap on `stat`
    n = len(xs)
    if n < 2: return float('nan')
    samples = [stat([xs[random.randrange(n)] for _ in range(n)]) for _ in range(B)]
    lo = percentile(samples, q); hi = percentile(samples, 100 - q)
    return (hi - lo) / 2.0

def summarize(label, all_raw):
    """all_raw: list of iter values (flattened)."""
    n = len(all_raw)
    m = statistics.mean(all_raw)
    sd = statistics.stdev(all_raw) if n > 1 else 0.0
    med = statistics.median(all_raw)
    tm10 = trimmed_mean(all_raw, 0.10)
    tm20 = trimmed_mean(all_raw, 0.20)
    iqr = percentile(all_raw, 75) - percentile(all_raw, 25)
    ci_mean = bootstrap_ci(all_raw, statistics.mean)
    ci_med = bootstrap_ci(all_raw, statistics.median)
    ci_tm10 = bootstrap_ci(all_raw, lambda xs: trimmed_mean(xs, 0.10))
    ci_tm20 = bootstrap_ci(all_raw, lambda xs: trimmed_mean(xs, 0.20))
    print(f"  {label:25s}  n={n:3d}")
    print(f"    mean   = {m:8.2f}  +/- {ci_mean:6.2f}  (95% boot)")
    print(f"    median = {med:8.2f}  +/- {ci_med:6.2f}")
    print(f"    tm10   = {tm10:8.2f}  +/- {ci_tm10:6.2f}")
    print(f"    tm20   = {tm20:8.2f}  +/- {ci_tm20:6.2f}")
    print(f"    sd     = {sd:8.2f}   IQR = {iqr:6.2f}")
    return dict(n=n, mean=m, median=med, tm10=tm10, tm20=tm20, sd=sd,
                ci_mean=ci_mean, ci_med=ci_med, ci_tm10=ci_tm10, ci_tm20=ci_tm20)

def fork_analysis(label, forks):
    fm = [statistics.mean(f) for f in forks]
    print(f"  per-fork means ({label}):", [f"{x:.1f}" for x in fm])
    if len(fm) > 1:
        print(f"    between-fork SD = {statistics.stdev(fm):8.2f}")
    wsds = [statistics.stdev(f) for f in forks if len(f) > 1]
    if wsds:
        print(f"    mean within-fork SD = {statistics.mean(wsds):8.2f}")
    # First-iter outlier check
    firsts = [f[0] for f in forks if len(f) > 0]
    rest = [v for f in forks for v in f[1:]]
    if firsts and rest:
        df = statistics.mean(firsts) - statistics.mean(rest)
        rest_sd = statistics.stdev(rest) if len(rest) > 1 else 0
        flag = " <-- WARMUP TAIL?" if rest_sd > 0 and df > 1.5 * rest_sd else ""
        print(f"    first-iter mean = {statistics.mean(firsts):8.2f}  rest mean = {statistics.mean(rest):8.2f}  diff = {df:+7.2f}{flag}")
    return fm

def compare(b_raw, p_raw):
    # Two-sample bootstrap on each estimator
    def boot_delta(stat, B=10000):
        nb, np_ = len(b_raw), len(p_raw)
        deltas = []
        for _ in range(B):
            bs = [b_raw[random.randrange(nb)] for _ in range(nb)]
            ps = [p_raw[random.randrange(np_)] for _ in range(np_)]
            deltas.append(stat(ps) - stat(bs))
        return statistics.mean(deltas), percentile(deltas, 2.5), percentile(deltas, 97.5)

    print()
    print("  Delta (post - baseline):")
    results = []
    for name, stat in [("mean", statistics.mean),
                       ("median", statistics.median),
                       ("tm10", lambda xs: trimmed_mean(xs, 0.10)),
                       ("tm20", lambda xs: trimmed_mean(xs, 0.20))]:
        bm = stat(b_raw)
        d, lo, hi = boot_delta(stat)
        pct = 100.0 * d / bm
        sig_bool = (lo > 0 or hi < 0)
        sig = "  ***SIG***" if sig_bool else "    (n.s.)"
        print(f"    {name:6s}: delta = {d:+7.2f} ms ({pct:+5.2f}%)   95% CI [{lo:+7.2f}, {hi:+7.2f}]{sig}")
        results.append((name, d, pct, lo, hi, sig_bool))

    # SD-ratio sanity guard. If the two runs have wildly different
    # within-run dispersion, the two-sample bootstrap CI can be
    # misleadingly tight — the smaller-SD run anchors the percentile
    # and the larger-SD run's outliers shift the mean. This pattern
    # (seen in iter 19: baseline SD 264 vs post SD 146, ratio 0.55)
    # produced a -14% delta that almost certainly overstates the
    # true effect. We flag any pair whose SD-ratio is outside
    # [0.6, 1.7] as INCONCLUSIVE regardless of significance.
    b_sd = statistics.stdev(b_raw) if len(b_raw) > 1 else 0.0
    p_sd = statistics.stdev(p_raw) if len(p_raw) > 1 else 0.0
    sd_ratio = (p_sd / b_sd) if b_sd > 0 else float('inf')
    SD_LO, SD_HI = 0.6, 1.7
    sd_ratio_ok = (SD_LO <= sd_ratio <= SD_HI)
    print(f"  SD-ratio (post/baseline) = {sd_ratio:.3f}   "
          f"(baseline sd={b_sd:.2f}, post sd={p_sd:.2f})   "
          f"acceptable=[{SD_LO}, {SD_HI}]   "
          f"{'OK' if sd_ratio_ok else '<-- OUT OF RANGE'}")

    # Verdict line: require >=3 of 4 estimators to agree on sign + significance
    sig_neg = sum(1 for r in results if r[5] and r[1] < 0)
    sig_pos = sum(1 for r in results if r[5] and r[1] > 0)
    base_verdict = None
    if sig_neg >= 3:
        base_verdict = "SPEEDUP (>=3/4 estimators significant in same direction) -- SHIP"
    elif sig_pos >= 3:
        base_verdict = "REGRESSION (>=3/4 estimators significant in same direction) -- REVERT"
    elif sig_neg >= 1 or sig_pos >= 1:
        base_verdict = f"MIXED ({sig_neg} sig speedup, {sig_pos} sig regression) -- INCONCLUSIVE, re-run with more forks"
    else:
        base_verdict = "NULL RESULT (no estimator significant) -- effect below noise floor"

    if not sd_ratio_ok:
        print(f"  WARN: SD-ratio {sd_ratio:.3f} outside [{SD_LO}, {SD_HI}]; "
              f"between-run variance is unstable so the bootstrap CI cannot be trusted.")
        print(f"  VERDICT: INCONCLUSIVE (SD-ratio guard tripped; base verdict was: {base_verdict}) -- re-run bench")
    else:
        print(f"  VERDICT: {base_verdict}")
    return results

def process(path, label, drop_n=1):
    d = load(path)
    forks = d['primaryMetric']['rawData']
    raw = [v for f in forks for v in f]
    print(f"{label}: {path}")
    print(f"  config: forks={d['forks']} iters={d['measurementIterations']} warmup={d['warmupIterations']}x{d['warmupTime']} meas-t={d['measurementTime']}")
    s_all = summarize("all iters", raw)
    fork_analysis(label, forks)
    # Drop-first-N rerun
    raw_df = [v for f in forks for v in f[drop_n:]]
    if raw_df and len(raw_df) < len(raw):
        print(f"  --- after dropping first {drop_n} iter(s) of each fork ({len(raw)-len(raw_df)} dropped) ---")
        s_df = summarize(f"drop-first-{drop_n}", raw_df)
    else:
        s_df = s_all
    return raw, raw_df

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--drop-first", type=int, default=1, dest="drop_first",
                    help="How many iters of each fork to drop in the drop-first rerun (default: 1).")
    ap.add_argument("--help", "-h", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()
    if args.help or not args.paths:
        print(__doc__); sys.exit(0 if args.help else 1)
    drop_n = max(0, args.drop_first)
    b_raw, b_df = process(args.paths[0], "baseline", drop_n=drop_n)
    if len(args.paths) >= 2:
        print()
        p_raw, p_df = process(args.paths[1], "postchange", drop_n=drop_n)
        print("\n=== Comparison (all iters) ===")
        compare(b_raw, p_raw)
        print(f"\n=== Comparison (drop-first-{drop_n}) ===")
        compare(b_df, p_df)

if __name__ == "__main__":
    main()
