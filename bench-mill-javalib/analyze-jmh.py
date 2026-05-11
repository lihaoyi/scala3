#!/usr/bin/env python3
"""Post-process JMH JSON: bootstrap CIs on mean/median/trimmed means, per-fork
SD breakdown, drop-first rerun, modified-Z outliers, two-sample or --paired
delta with a SHIP/REVERT/MIXED/NULL verdict. Use --help for full flag list.
"""
from __future__ import annotations
import argparse, json, math, statistics, sys, random
from pathlib import Path

random.seed(20260511)

# ----- IO ---------------------------------------------------------------

def load_all(p):
    with open(p) as f:
        return json.load(f)

def load(p):
    """Return the FIRST benchmark entry. Preserves legacy callers."""
    return load_all(p)[0]

# ----- Stats primitives -------------------------------------------------

def percentile(xs, q):
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

def tm10(xs): return trimmed_mean(xs, 0.10)
def tm20(xs): return trimmed_mean(xs, 0.20)

ESTIMATORS = [
    ("mean",   statistics.mean),
    ("median", statistics.median),
    ("tm10",   tm10),
    ("tm20",   tm20),
]

# ----- Normal-distribution helpers (stdlib only) ------------------------

def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def _norm_ppf(p):
    # Beasley-Springer-Moro approximation; good to ~1e-7 in the tails we need.
    # Avoid degeneracies at 0/1.
    if p <= 0.0: return -math.inf
    if p >= 1.0: return math.inf
    # Use the inverse of the standard normal CDF via rational approximation.
    # Reference: Acklam's algorithm.
    a = [-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00]
    plow = 0.02425; phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p <= phigh:
        q = p - 0.5; r = q*q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)

# ----- Bootstrap CIs ----------------------------------------------------

def _resample(xs, n):
    return [xs[random.randrange(n)] for _ in range(n)]

def bootstrap_samples(xs, stat, B):
    n = len(xs)
    return [stat(_resample(xs, n)) for _ in range(B)]

def percentile_ci(samples, alpha=0.05):
    lo = percentile(samples, 100 * alpha / 2)
    hi = percentile(samples, 100 * (1 - alpha / 2))
    return lo, hi

def bca_ci(xs, stat, samples, alpha=0.05):
    """BCa CI for stat(xs) given precomputed bootstrap `samples`."""
    n = len(xs)
    if n < 3:
        return percentile_ci(samples, alpha)
    theta_hat = stat(xs)
    # Bias correction z0
    less = sum(1 for s in samples if s < theta_hat)
    p = less / len(samples)
    # Clamp to avoid +-inf
    p = min(max(p, 1.0/(2*len(samples))), 1 - 1.0/(2*len(samples)))
    z0 = _norm_ppf(p)
    # Acceleration a via jackknife
    jk = []
    for i in range(n):
        jk.append(stat(xs[:i] + xs[i+1:]))
    jk_mean = statistics.mean(jk)
    num = sum((jk_mean - v) ** 3 for v in jk)
    den = 6.0 * (sum((jk_mean - v) ** 2 for v in jk) ** 1.5)
    a = num / den if den != 0 else 0.0
    z_a2 = _norm_ppf(alpha / 2)
    z_1ma2 = _norm_ppf(1 - alpha / 2)
    def adj(z):
        denom = 1 - a * (z0 + z)
        if denom == 0: return 0.5
        return _norm_cdf(z0 + (z0 + z) / denom)
    p_lo = adj(z_a2)
    p_hi = adj(z_1ma2)
    lo = percentile(samples, 100 * p_lo)
    hi = percentile(samples, 100 * p_hi)
    return lo, hi

def ci_for(xs, stat, B=10000, method='bca', alpha=0.05):
    n = len(xs)
    if n < 2:
        return float('nan'), float('nan'), float('nan')
    samples = bootstrap_samples(xs, stat, B)
    if method == 'percentile':
        lo, hi = percentile_ci(samples, alpha)
    else:
        lo, hi = bca_ci(xs, stat, samples, alpha)
    half = (hi - lo) / 2.0
    return lo, hi, half

# ----- Outlier detection ------------------------------------------------

def modified_z_outliers(xs, threshold=3.5):
    """Return indices of xs whose modified-Z exceeds `threshold`."""
    n = len(xs)
    if n < 3: return []
    med = statistics.median(xs)
    mad = statistics.median([abs(v - med) for v in xs])
    if mad == 0:
        # Fall back to mean+SD if all values cluster on the median
        mu = statistics.mean(xs); sd = statistics.pstdev(xs)
        if sd == 0: return []
        return [i for i, v in enumerate(xs) if abs(v - mu) / sd > threshold]
    return [i for i, v in enumerate(xs) if 0.6745 * abs(v - med) / mad > threshold]

# ----- Display ----------------------------------------------------------

def summarize(label, all_raw, B=10000, method='bca'):
    n = len(all_raw)
    m = statistics.mean(all_raw)
    sd = statistics.stdev(all_raw) if n > 1 else 0.0
    med = statistics.median(all_raw)
    t10 = tm10(all_raw); t20 = tm20(all_raw)
    iqr = percentile(all_raw, 75) - percentile(all_raw, 25)
    _, _, ci_mean = ci_for(all_raw, statistics.mean, B=B, method=method)
    _, _, ci_med  = ci_for(all_raw, statistics.median, B=B, method=method)
    _, _, ci_t10  = ci_for(all_raw, tm10, B=B, method=method)
    _, _, ci_t20  = ci_for(all_raw, tm20, B=B, method=method)
    print(f"  {label:25s}  n={n:3d}")
    print(f"    mean   = {m:8.2f}  +/- {ci_mean:6.2f}  (95% {method})")
    print(f"    median = {med:8.2f}  +/- {ci_med:6.2f}")
    print(f"    tm10   = {t10:8.2f}  +/- {ci_t10:6.2f}")
    print(f"    tm20   = {t20:8.2f}  +/- {ci_t20:6.2f}")
    print(f"    sd     = {sd:8.2f}   IQR = {iqr:6.2f}")
    return dict(n=n, mean=m, median=med, tm10=t10, tm20=t20, sd=sd,
                ci_mean=ci_mean, ci_med=ci_med, ci_tm10=ci_t10, ci_tm20=ci_t20)

def fork_analysis(label, forks):
    fm = [statistics.mean(f) for f in forks]
    print(f"  per-fork means ({label}):", [f"{x:.1f}" for x in fm])
    if len(fm) > 1:
        print(f"    between-fork SD = {statistics.stdev(fm):8.2f}")
    wsds = [statistics.stdev(f) for f in forks if len(f) > 1]
    if wsds:
        print(f"    mean within-fork SD = {statistics.mean(wsds):8.2f}")
    firsts = [f[0] for f in forks if len(f) > 0]
    rest = [v for f in forks for v in f[1:]]
    if firsts and rest:
        df = statistics.mean(firsts) - statistics.mean(rest)
        rest_sd = statistics.stdev(rest) if len(rest) > 1 else 0
        flag = " <-- WARMUP TAIL?" if rest_sd > 0 and df > 1.5 * rest_sd else ""
        print(f"    first-iter mean = {statistics.mean(firsts):8.2f}  rest mean = {statistics.mean(rest):8.2f}  diff = {df:+7.2f}{flag}")
    return fm

# ----- Two-sample bootstrap on a delta ----------------------------------

def two_sample_delta_samples(b_raw, p_raw, stat, B):
    nb, np_ = len(b_raw), len(p_raw)
    out = []
    for _ in range(B):
        bs = _resample(b_raw, nb)
        ps = _resample(p_raw, np_)
        out.append(stat(ps) - stat(bs))
    return out

def two_sample_delta_ci(b_raw, p_raw, stat, B=10000, method='bca', alpha=0.05):
    samples = two_sample_delta_samples(b_raw, p_raw, stat, B)
    point = stat(p_raw) - stat(b_raw)
    if method == 'percentile':
        lo, hi = percentile_ci(samples, alpha)
        return point, lo, hi
    # BCa on the combined-resampling distribution: bias correction from the
    # bootstrap distribution itself; acceleration via jackknife over the
    # *combined* index list, dropping one observation at a time and
    # recomputing the delta from the leave-one-out side.
    less = sum(1 for s in samples if s < point)
    fp = less / len(samples)
    fp = min(max(fp, 1.0/(2*len(samples))), 1 - 1.0/(2*len(samples)))
    z0 = _norm_ppf(fp)
    # Jackknife: leave-one-out within each group, average sensitivity.
    jk = []
    nb_ = len(b_raw); np2 = len(p_raw)
    for i in range(nb_):
        jk.append(stat(p_raw) - stat(b_raw[:i] + b_raw[i+1:]))
    for j in range(np2):
        jk.append(stat(p_raw[:j] + p_raw[j+1:]) - stat(b_raw))
    jk_mean = statistics.mean(jk)
    num = sum((jk_mean - v) ** 3 for v in jk)
    den = 6.0 * (sum((jk_mean - v) ** 2 for v in jk) ** 1.5)
    a = num / den if den != 0 else 0.0
    z_a2 = _norm_ppf(alpha / 2); z_1ma2 = _norm_ppf(1 - alpha / 2)
    def adj(z):
        denom = 1 - a * (z0 + z)
        if denom == 0: return 0.5
        return _norm_cdf(z0 + (z0 + z) / denom)
    lo = percentile(samples, 100 * adj(z_a2))
    hi = percentile(samples, 100 * adj(z_1ma2))
    return point, lo, hi

# ----- Paired bootstrap on per-pair deltas ------------------------------

def paired_delta_ci(deltas, stat, B=10000, method='bca', alpha=0.05):
    samples = bootstrap_samples(deltas, stat, B)
    point = stat(deltas)
    if method == 'percentile':
        lo, hi = percentile_ci(samples, alpha)
    else:
        lo, hi = bca_ci(deltas, stat, samples, alpha)
    return point, lo, hi

# ----- Sample-size and MDE ----------------------------------------------

Z_975 = 1.959963984540054   # 97.5% of N(0,1)
Z_80  = 0.8416212335729143  # 80% of N(0,1)

def n_for_two_sample(sd_b, sd_p, half_abs):
    # Two-sample mean delta SE^2 = sd_b^2/n + sd_p^2/n  (equal n). Half = z * SE.
    if half_abs <= 0: return float('inf')
    var = sd_b**2 + sd_p**2
    return (Z_975 ** 2) * var / (half_abs ** 2)

def n_for_paired(sd_d, half_abs):
    if half_abs <= 0: return float('inf')
    return (Z_975 ** 2) * (sd_d ** 2) / (half_abs ** 2)

def mde_two_sample(sd_b, sd_p, n):
    if n <= 0: return float('inf')
    se = math.sqrt(sd_b**2/n + sd_p**2/n)
    return (Z_975 + Z_80) * se

def mde_paired(sd_d, n):
    if n <= 0: return float('inf')
    se = sd_d / math.sqrt(n)
    return (Z_975 + Z_80) * se

# ----- Comparison block (two-sample) ------------------------------------

def compare(b_raw, p_raw, *, B=10000, method='bca',
            target_ci_width=None, mde=False, sd_guard=True, mode_label='two-sample'):
    print()
    print(f"  Delta (post - baseline) [{mode_label}, CI={method}]:")
    results = []
    base_mean = statistics.mean(b_raw)
    for name, stat in ESTIMATORS:
        d, lo, hi = two_sample_delta_ci(b_raw, p_raw, stat, B=B, method=method)
        pct = 100.0 * d / base_mean
        half = (hi - lo) / 2.0
        half_pct = 100.0 * half / base_mean
        sig_bool = (lo > 0 or hi < 0)
        sig = "  ***SIG***" if sig_bool else "    (n.s.)"
        print(f"    {name:6s}: delta = {d:+8.2f} ms ({pct:+6.2f}%)   95% CI [{lo:+8.2f}, {hi:+8.2f}] (+/- {half_pct:5.2f}%){sig}")
        results.append((name, d, pct, lo, hi, sig_bool, half_pct))

    b_sd = statistics.stdev(b_raw) if len(b_raw) > 1 else 0.0
    p_sd = statistics.stdev(p_raw) if len(p_raw) > 1 else 0.0
    sd_ratio = (p_sd / b_sd) if b_sd > 0 else float('inf')
    SD_LO, SD_HI = 0.6, 1.7
    sd_ratio_ok = (SD_LO <= sd_ratio <= SD_HI)
    print(f"  SD-ratio (post/baseline) = {sd_ratio:.3f}   "
          f"(baseline sd={b_sd:.2f}, post sd={p_sd:.2f})   "
          f"acceptable=[{SD_LO}, {SD_HI}]   "
          f"{'OK' if sd_ratio_ok else '<-- OUT OF RANGE'}")

    _emit_verdict(results, sd_ratio, sd_ratio_ok, sd_guard=sd_guard,
                  paired=False, target_ci_width=target_ci_width)

    if target_ci_width is not None:
        target_abs = target_ci_width / 100.0 * base_mean
        n_ts = n_for_two_sample(b_sd, p_sd, target_abs)
        # For the paired projection we don't have actual paired data here -
        # but we can give an UPPER bound by assuming zero correlation
        # (paired SD = sqrt(sd_b^2 + sd_p^2)) and a LOWER bound for an
        # arbitrary rho. The most useful single number is the rho=0 estimate
        # which equals the two-sample number; report both with a note.
        print(f"  --- sample-size for +/- {target_ci_width:.2f}% CI half-width on delta ---")
        print(f"    two-sample (equal n per side):  n >= {math.ceil(n_ts):>5d} iters/side  "
              f"(target abs = {target_abs:.2f} ms)")
        print(f"    paired (rho=0 upper bound)     : n >= {math.ceil(n_ts):>5d} pairs")
        print(f"    paired (rho=0.5)               : n >= {math.ceil(n_ts/2):>5d} pairs")
        print(f"    paired (rho=0.9)               : n >= {math.ceil(n_ts/10):>5d} pairs")
        print( "    (paired n needs ACTUAL paired data to estimate exactly;")
        print( "     re-run with --paired once interleaved data is available.)")

    if mde:
        n_eq = min(len(b_raw), len(p_raw))
        mde_abs = mde_two_sample(b_sd, p_sd, n_eq)
        print(f"  MDE @ 80% power, n={n_eq}/side: {mde_abs:.2f} ms ({100.0*mde_abs/base_mean:+.2f}% of baseline)")

    return results

def compare_paired(deltas, b_for_pct, *, B=10000, method='bca',
                   target_ci_width=None, mde=False):
    print()
    print(f"  Paired delta (per-pair: post - baseline) [paired, CI={method}, n_pairs={len(deltas)}]:")
    results = []
    base_mean = statistics.mean(b_for_pct)
    sd_d = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
    for name, stat in ESTIMATORS:
        d, lo, hi = paired_delta_ci(deltas, stat, B=B, method=method)
        pct = 100.0 * d / base_mean
        half = (hi - lo) / 2.0
        half_pct = 100.0 * half / base_mean
        sig_bool = (lo > 0 or hi < 0)
        sig = "  ***SIG***" if sig_bool else "    (n.s.)"
        print(f"    {name:6s}: delta = {d:+8.2f} ms ({pct:+6.2f}%)   95% CI [{lo:+8.2f}, {hi:+8.2f}] (+/- {half_pct:5.2f}%){sig}")
        results.append((name, d, pct, lo, hi, sig_bool, half_pct))
    print(f"  paired-delta SD = {sd_d:.2f} ms  (vs raw baseline SD)")
    print(f"  (paired mode bypasses the SD-ratio guard: paired bootstrap is robust to differing group variances)")

    _emit_verdict(results, sd_ratio=1.0, sd_ratio_ok=True, sd_guard=False,
                  paired=True, target_ci_width=target_ci_width)

    if target_ci_width is not None:
        target_abs = target_ci_width / 100.0 * base_mean
        n_p = n_for_paired(sd_d, target_abs)
        print(f"  --- sample-size for +/- {target_ci_width:.2f}% CI half-width on delta ---")
        print(f"    paired (observed sd_d={sd_d:.2f}): n >= {math.ceil(n_p):>5d} pairs  "
              f"(target abs = {target_abs:.2f} ms)")

    if mde:
        mde_abs = mde_paired(sd_d, len(deltas))
        print(f"  MDE @ 80% power, n_pairs={len(deltas)}: {mde_abs:.2f} ms ({100.0*mde_abs/base_mean:+.2f}% of baseline)")

    return results

def _emit_verdict(results, sd_ratio, sd_ratio_ok, *, sd_guard, paired, target_ci_width):
    sig_neg = sum(1 for r in results if r[5] and r[1] < 0)
    sig_pos = sum(1 for r in results if r[5] and r[1] > 0)
    if sig_neg >= 3:
        base_verdict = "SPEEDUP (>=3/4 estimators significant in same direction) -- SHIP"
    elif sig_pos >= 3:
        base_verdict = "REGRESSION (>=3/4 estimators significant in same direction) -- REVERT"
    elif sig_neg >= 1 or sig_pos >= 1:
        base_verdict = f"MIXED ({sig_neg} sig speedup, {sig_pos} sig regression) -- INCONCLUSIVE, re-run with more forks"
    else:
        base_verdict = "NULL RESULT (no estimator significant) -- effect below noise floor"

    # Underpowered note: if CI half-width (mean estimator) exceeds the target
    # threshold (default 1% if --target-ci-width not given), tack on a note.
    threshold = target_ci_width if target_ci_width is not None else 1.0
    mean_half_pct = next((r[6] for r in results if r[0] == 'mean'), float('inf'))
    underpowered = mean_half_pct > threshold

    if sd_guard and not paired and not sd_ratio_ok:
        print(f"  WARN: SD-ratio {sd_ratio:.3f} out of band; "
              f"between-run variance is unstable so the two-sample bootstrap CI cannot be trusted.")
        verdict = f"INCONCLUSIVE (SD-ratio guard tripped; base verdict was: {base_verdict}) -- re-run bench"
    else:
        verdict = base_verdict

    if underpowered:
        verdict = f"{verdict}  [UNDERPOWERED -- mean-CI half-width {mean_half_pct:.2f}% > {threshold:.2f}%; increase N]"

    print(f"  VERDICT: {verdict}")

# ----- Process one side -------------------------------------------------

def process(path_or_entry, label, drop_n=1, *, B=10000, method='bca',
            outlier_rerun=True):
    if isinstance(path_or_entry, dict):
        d = path_or_entry
        print(f"{label}: <entry from {d.get('benchmark','?')}>  params={d.get('params')}")
    else:
        d = load(path_or_entry)
        print(f"{label}: {path_or_entry}")
    forks = d['primaryMetric']['rawData']
    raw = [v for f in forks for v in f]
    print(f"  config: forks={d['forks']} iters={d['measurementIterations']} warmup={d['warmupIterations']}x{d['warmupTime']} meas-t={d['measurementTime']}")
    s_all = summarize("all iters", raw, B=B, method=method)
    fork_analysis(label, forks)

    # Modified-Z outliers across the flattened iter list
    outlier_idx = modified_z_outliers(raw, threshold=3.5)
    if outlier_idx:
        ov = [(i, raw[i]) for i in outlier_idx]
        print(f"    modified-Z outliers (threshold=3.5): {ov}")
    else:
        print(f"    modified-Z outliers (threshold=3.5): none")

    raw_no_out = [v for i, v in enumerate(raw) if i not in set(outlier_idx)]
    if outlier_rerun and outlier_idx and len(raw_no_out) >= 3:
        print(f"  --- after dropping {len(outlier_idx)} outlier iter(s) ---")
        summarize("no-outliers", raw_no_out, B=B, method=method)

    raw_df = [v for f in forks for v in f[drop_n:]]
    if raw_df and len(raw_df) < len(raw):
        print(f"  --- after dropping first {drop_n} iter(s) of each fork ({len(raw)-len(raw_df)} dropped) ---")
        s_df = summarize(f"drop-first-{drop_n}", raw_df, B=B, method=method)
    else:
        s_df = s_all
    return raw, raw_df, raw_no_out, outlier_idx

# ----- Paired helpers ---------------------------------------------------

PAIRED_PARAM_KEYS = ("compiler", "branch", "variant", "side", "build")

def split_paired_single_file(path):
    """Given a single JSON file with two benchmark entries differing only in
    a known @Param, return (baseline_raw, postchange_raw, baseline_label,
    postchange_label, entry_for_summary_baseline, entry_for_summary_post)."""
    entries = load_all(path)
    if len(entries) < 2:
        raise SystemExit(f"--paired with one file requires >=2 benchmark entries, got {len(entries)} in {path}")
    # Detect the param key
    pkey = None
    for e in entries:
        params = e.get('params') or {}
        for k in PAIRED_PARAM_KEYS:
            if k in params:
                pkey = k; break
        if pkey: break
    if pkey is None:
        raise SystemExit(
            f"--paired single-file mode: no @Param found in entries (looked for {PAIRED_PARAM_KEYS}).\n"
            f"  Entries had params: {[e.get('params') for e in entries]}\n"
            f"  Either add a @Param to your JMH benchmark or pass two files."
        )
    by_val = {}
    for e in entries:
        v = (e.get('params') or {}).get(pkey)
        by_val.setdefault(v, []).append(e)
    if len(by_val) != 2:
        raise SystemExit(f"--paired single-file mode: expected exactly 2 values of @Param `{pkey}`, found {list(by_val)}")
    # Heuristic: prefer 'baseline'/'main' as baseline; otherwise sort.
    keys = list(by_val.keys())
    preferred = ('baseline', 'main', 'before', 'pre', 'a')
    keys.sort(key=lambda v: (preferred.index(str(v).lower()) if str(v).lower() in preferred else 99, str(v)))
    bkey, pkey_v = keys[0], keys[1]
    # Concatenate raw across all entries with that param value (typically one
    # entry per value, but if --forks N is used the harness MAY emit them
    # separately; be defensive).
    def flat(entries_for_val):
        out_forks = []
        for e in entries_for_val:
            out_forks.extend(e['primaryMetric']['rawData'])
        return out_forks
    bforks = flat(by_val[bkey])
    pforks = flat(by_val[pkey_v])
    return bforks, pforks, str(bkey), str(pkey_v), by_val[bkey][0], by_val[pkey_v][0]

def pair_iterations(b_forks, p_forks):
    """Flatten then pair iter i of baseline with iter i of postchange. If
    lengths differ, truncate to the shorter and emit a warning."""
    b_flat = [v for f in b_forks for v in f]
    p_flat = [v for f in p_forks for v in f]
    nb, np_ = len(b_flat), len(p_flat)
    if nb != np_:
        m = min(nb, np_)
        sys.stderr.write(f"WARN: paired iteration count mismatch (baseline={nb}, post={np_}); truncating to {m}.\n")
        b_flat = b_flat[:m]; p_flat = p_flat[:m]
    deltas = [p - b for b, p in zip(b_flat, p_flat)]
    return b_flat, p_flat, deltas

# ----- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--drop-first", type=int, default=1, dest="drop_first")
    ap.add_argument("--paired", action="store_true")
    ap.add_argument("--ci-method", choices=("bca", "percentile"), default="bca")
    ap.add_argument("--boot", type=int, default=10000)
    ap.add_argument("--target-ci-width", type=float, default=None,
                    help="Target 95%% CI half-width on the delta, as %% of baseline.")
    ap.add_argument("--mde", action="store_true")
    ap.add_argument("--no-outlier-rerun", dest="outlier_rerun", action="store_false")
    ap.add_argument("--no-sd-guard", dest="sd_guard", action="store_false")
    ap.add_argument("--help", "-h", action="store_true")
    ap.add_argument("paths", nargs="*")
    ap.set_defaults(outlier_rerun=True, sd_guard=True)
    args = ap.parse_args()
    if args.help or not args.paths:
        print(__doc__); sys.exit(0 if args.help else 1)
    drop_n = max(0, args.drop_first)
    B = max(1000, args.boot)
    method = args.ci_method

    if args.paired:
        # Two possible shapes
        if len(args.paths) == 1:
            b_forks, p_forks, blabel, plabel, b_entry, p_entry = split_paired_single_file(args.paths[0])
            b_raw, b_df, b_nout, _ = process(b_entry, f"baseline ({blabel})", drop_n=drop_n,
                                              B=B, method=method, outlier_rerun=args.outlier_rerun)
            print()
            p_raw, p_df, p_nout, _ = process(p_entry, f"postchange ({plabel})", drop_n=drop_n,
                                              B=B, method=method, outlier_rerun=args.outlier_rerun)
            b_flat, p_flat, deltas = pair_iterations(b_forks, p_forks)
        elif len(args.paths) == 2:
            b_raw, b_df, b_nout, _ = process(args.paths[0], "baseline", drop_n=drop_n,
                                              B=B, method=method, outlier_rerun=args.outlier_rerun)
            print()
            p_raw, p_df, p_nout, _ = process(args.paths[1], "postchange", drop_n=drop_n,
                                              B=B, method=method, outlier_rerun=args.outlier_rerun)
            b_entry = load(args.paths[0]); p_entry = load(args.paths[1])
            b_flat, p_flat, deltas = pair_iterations(
                b_entry['primaryMetric']['rawData'],
                p_entry['primaryMetric']['rawData'])
        else:
            raise SystemExit("--paired takes 1 or 2 path arguments")

        print("\n=== Paired comparison (all iters, iter-by-iter pairing) ===")
        compare_paired(deltas, b_flat, B=B, method=method,
                       target_ci_width=args.target_ci_width, mde=args.mde)

        # Drop-first analogue for paired: drop first N from each fork before
        # pairing. Since pair_iterations already flattened, redo with sliced
        # raw_df from each side.
        if drop_n > 0:
            # Re-derive flat sequences from the raw forks with drop-first
            def drop_flat(entry_or_forks):
                forks = entry_or_forks if isinstance(entry_or_forks, list) else entry_or_forks['primaryMetric']['rawData']
                return [v for f in forks for v in f[drop_n:]]
            b_forks_for_df = b_entry['primaryMetric']['rawData'] if isinstance(b_entry, dict) else b_entry
            p_forks_for_df = p_entry['primaryMetric']['rawData'] if isinstance(p_entry, dict) else p_entry
            b_df_flat = drop_flat(b_forks_for_df); p_df_flat = drop_flat(p_forks_for_df)
            m = min(len(b_df_flat), len(p_df_flat))
            if m >= 3:
                deltas_df = [p - b for b, p in zip(b_df_flat[:m], p_df_flat[:m])]
                print(f"\n=== Paired comparison (drop-first-{drop_n}) ===")
                compare_paired(deltas_df, b_df_flat[:m], B=B, method=method,
                               target_ci_width=args.target_ci_width, mde=args.mde)
        return

    # Two-sample (legacy) path
    b_raw, b_df, b_nout, b_outl = process(args.paths[0], "baseline", drop_n=drop_n,
                                           B=B, method=method, outlier_rerun=args.outlier_rerun)
    if len(args.paths) >= 2:
        print()
        p_raw, p_df, p_nout, p_outl = process(args.paths[1], "postchange", drop_n=drop_n,
                                               B=B, method=method, outlier_rerun=args.outlier_rerun)
        print("\n=== Comparison (all iters) ===")
        compare(b_raw, p_raw, B=B, method=method,
                target_ci_width=args.target_ci_width, mde=args.mde,
                sd_guard=args.sd_guard)
        print(f"\n=== Comparison (drop-first-{drop_n}) ===")
        compare(b_df, p_df, B=B, method=method,
                target_ci_width=args.target_ci_width, mde=args.mde,
                sd_guard=args.sd_guard)
        if args.outlier_rerun and (b_outl or p_outl) and len(b_nout) >= 3 and len(p_nout) >= 3:
            print(f"\n=== Comparison (outliers removed: baseline -{len(b_outl)}, post -{len(p_outl)}) ===")
            compare(b_nout, p_nout, B=B, method=method,
                    target_ci_width=args.target_ci_width, mde=args.mde,
                    sd_guard=args.sd_guard)

if __name__ == "__main__":
    main()
