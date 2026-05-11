#!/usr/bin/env python3
"""Aggregate `jdk.ExecutionSample` events from a JFR file and print top
hot methods by self time (top of stack) and inclusive time (anywhere in stack).
"""
from __future__ import annotations
import argparse, collections, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from _common import BUILD_DIR

DEFAULT_JFR = BUILD_DIR / "profile" / "profile.jfr"
JFR_CANDIDATES = [
    "/Library/Java/JavaVirtualMachines/amazon-corretto-21.jdk/Contents/Home/bin/jfr",
    "/Users/lihaoyi/Library/Java/JavaVirtualMachines/jbr-21.0.9/Contents/Home/bin/jfr",
]
SKIP_PREFIXES = (
    "java.", "jdk.internal.", "sun.", "com.sun.",
    "scala.collection.", "scala.runtime.", "scala.Function", "scala.Tuple",
)
TS_RE = re.compile(r"startTime\s*=\s*(\d{2}):(\d{2}):(\d{2})\.(\d+)")

def find_jfr_bin() -> str:
    for c in JFR_CANDIDATES:
        if Path(c).is_file():
            return c
    found = shutil.which("jfr")
    if found:
        return found
    sys.exit("jfr CLI not found; install JDK 21 or set PATH")

def hms_ms_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jfr", type=Path, default=DEFAULT_JFR)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--skip-warmup-sec", type=float, default=0.0)
    ap.add_argument("--filter", default="")
    ap.add_argument("--no-jdk", action="store_true")
    ap.add_argument("--thread", default="main")
    ap.add_argument("--all-threads", action="store_true")
    args = ap.parse_args()
    if not args.jfr.is_file():
        sys.exit(f"JFR file not found: {args.jfr}")

    print(f"[analyze-jfr] reading {args.jfr} ...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        subprocess.run(
            [find_jfr_bin(), "print",
             "--events", "jdk.ExecutionSample",
             "--stack-depth", "64", str(args.jfr)],
            stdout=tmp, check=True,
        )
        tmp_path = Path(tmp.name)

    try:
        text = tmp_path.read_text()
    finally:
        tmp_path.unlink(missing_ok=True)
    events = text.split("jdk.ExecutionSample {")[1:]

    first_t = None
    m0 = TS_RE.search(text)
    if m0:
        first_t = hms_ms_seconds(*m0.groups())

    own: collections.Counter[str] = collections.Counter()
    total: collections.Counter[str] = collections.Counter()
    kept = dropped_thread = dropped_warmup = 0

    for ev in events:
        m_thr = re.search(r'sampledThread\s*=\s*"([^"]+)"', ev)
        thr = m_thr.group(1) if m_thr else ""
        if not args.all_threads and thr != args.thread:
            dropped_thread += 1
            continue
        if args.skip_warmup_sec and first_t is not None:
            m_ts = TS_RE.search(ev)
            if m_ts and hms_ms_seconds(*m_ts.groups()) - first_t < args.skip_warmup_sec:
                dropped_warmup += 1
                continue
        m_st = re.search(r"stackTrace\s*=\s*\[(.*?)\]\s*\}", ev, re.S)
        if not m_st:
            continue
        frames: list[str] = []
        for line in m_st.group(1).splitlines():
            s = line.strip()
            if not s:
                continue
            paren = s.find("(")
            if paren == -1:
                continue
            method = s[:paren]
            if args.no_jdk and method.startswith(SKIP_PREFIXES):
                continue
            if args.filter and args.filter not in method:
                continue
            frames.append(method)
        if not frames:
            continue
        kept += 1
        own[frames[0]] += 1
        seen: set[str] = set()
        for f in frames:
            if f in seen:
                continue
            seen.add(f)
            total[f] += 1

    print("\n=== JFR analysis ===")
    print(f"Total samples kept: {kept}")
    print(f"  dropped (thread != {args.thread}): {dropped_thread}")
    print(f"  dropped (warmup < {args.skip_warmup_sec}s): {dropped_warmup}")
    print(f"  filter: {args.filter or '(none)'}, no_jdk: {args.no_jdk}, all_threads: {args.all_threads}")
    if kept == 0:
        return

    def table(title: str, counter: collections.Counter[str]) -> None:
        print(f"\n{title}")
        print(f"  {'%':>6} {'count':>7}  method")
        for method, n in counter.most_common(args.top):
            print(f"  {100 * n / kept:6.2f} {n:7d}  {method}")

    table("=== Top by SELF time (sample at top of stack) ===", own)
    table("=== Top by TOTAL/INCLUSIVE time (sample anywhere in stack) ===", total)

if __name__ == "__main__":
    main()
