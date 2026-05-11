#!/usr/bin/env python3
"""Run one short JMH bench and print mean/SD/RSD/CI. Defaults to 3 warmup +
10 measurement iters (drop-first-3 for stats).
"""
from __future__ import annotations
import argparse, json, statistics, subprocess, sys, time
from pathlib import Path
from _common import SCRIPT_DIR, BUILD_DIR

def fmt_row(label: str, xs: list[float]) -> str:
    n = len(xs)
    if n == 0:
        return f"  {label}: N=0"
    m = statistics.mean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    ci = 1.96 * sd / n ** 0.5
    return (f"  {label}: N={n} mean={m:.1f}ms sd={sd:.1f}ms RSD={100*sd/m:.2f}% "
            f"min={min(xs):.0f} max={max(xs):.0f}  mean-CI ±{ci:.1f}ms (±{100*ci/m:.2f}%)")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--iters", type=int, default=10, dest="iterations")
    ap.add_argument("--time", type=int, default=5)
    ap.add_argument("--warmup-time", type=int, default=5)
    ap.add_argument("--forks", type=int, default=1)
    ap.add_argument("--drop-first", type=int, default=3)
    ap.add_argument("--jvm-extra", default="")
    ap.add_argument("--jmh-extra", default="")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    out_json = BUILD_DIR / f"jmh-{args.name}.json"
    out_log = BUILD_DIR / f"bench-{args.name}.log"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    for p in (out_json, out_log):
        if p.exists():
            p.unlink()

    cmd = [
        sys.executable, str(SCRIPT_DIR / "run-bench.py"),
        "--warmup", str(args.warmup),
        "--warmup-time", str(args.warmup_time),
        "--iterations", str(args.iterations),
        "--time", str(args.time),
        "--forks", str(args.forks),
    ]
    if args.skip_build:
        cmd.append("--skip-build")
    if args.jvm_extra:
        cmd += ["--jvm-extra", args.jvm_extra]
    cmd += ["--", "-rf", "json", "-rff", str(out_json), *args.jmh_extra.split()]

    print(f"[quick-bench] {args.name}: warmup={args.warmup}x{args.warmup_time}s "
          f"iters={args.iterations}x{args.time}s forks={args.forks} "
          f"jvm='{args.jvm_extra}' jmh='{args.jmh_extra}'")
    t0 = time.time()
    with open(out_log, "w") as logf:
        proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    elapsed = int(time.time() - t0)
    if proc.returncode != 0 or not out_json.exists() or out_json.stat().st_size == 0:
        sys.stderr.write(f"[quick-bench] {args.name}: FAILED rc={proc.returncode} "
                         f"elapsed={elapsed}s\n")
        with open(out_log) as f:
            sys.stderr.write("".join(f.readlines()[-20:]))
        sys.exit(1)

    data = json.loads(out_json.read_text())[0]
    forks = data["primaryMetric"]["rawData"]
    nforks = len(forks)
    all_iters = [v for f in forks for v in f]
    tail = [v for f in forks for v in f[args.drop_first:]]

    print(f"[quick-bench] {args.name}: elapsed={elapsed}s nForks={nforks}")
    print(fmt_row("all-iters             ", all_iters))
    print(fmt_row(f"post-warmup (drop-{args.drop_first})", tail))
    if nforks > 1:
        per_fork = [statistics.mean(f[args.drop_first:]) for f in forks if len(f) > args.drop_first]
        bsd = statistics.stdev(per_fork) if len(per_fork) > 1 else 0.0
        print(f"  per-fork tail means: {[f'{x:.0f}' for x in per_fork]}  between-fork sd={bsd:.1f}ms")
    for fi, f in enumerate(forks):
        prefix = f"  fork {fi}: " if nforks > 1 else "  iters:  "
        parts = [f"{v:7.1f}{'*' if i < args.drop_first else ''}" for i, v in enumerate(f)]
        print(prefix + " | ".join(parts) + "    (* = dropped)")

if __name__ == "__main__":
    main()
