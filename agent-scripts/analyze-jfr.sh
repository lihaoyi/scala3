#!/usr/bin/env bash
#
# analyze-jfr.sh — Aggregate ExecutionSample events from a JFR file and
# print top hot methods by:
#   1) "self" / "own" time   = method appears at TOP of stack
#   2) "total" / "inclusive" = method appears ANYWHERE in stack
#
# Both metrics are reported as percentage of all execution samples and
# absolute count. By default we count main-thread samples only, since
# we care about compiler work, not GC threads etc.
#
# Optional args:
#   --jfr PATH         JFR file to analyze (default: bench-mill-javalib/build/profile/profile.jfr)
#   --top N            Number of rows to print (default 30)
#   --skip-warmup-sec S  Skip the first S seconds of samples (warmup) (default 0)
#   --filter STR       Only include methods whose fully-qualified name contains STR
#   --no-jdk           Drop java.* / jdk.* / sun.* frames from samples (focus on dotc)
#   --thread NAME      Only consider samples from threads with this Java name (default "main")
#   --all-threads      Include samples from all threads
#
# Requires: jfr CLI (Java 21 JDK), python3.

set -euo pipefail

SCALA3_DIR="/Users/lihaoyi/Github/scala3"
DEFAULT_JFR="${SCALA3_DIR}/bench-mill-javalib/build/profile/profile.jfr"

JFR_FILE="${DEFAULT_JFR}"
TOP=30
SKIP_SEC=0
FILTER=""
NO_JDK=0
THREAD_NAME="main"
ALL_THREADS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --jfr) JFR_FILE="$2"; shift 2 ;;
    --top) TOP="$2"; shift 2 ;;
    --skip-warmup-sec) SKIP_SEC="$2"; shift 2 ;;
    --filter) FILTER="$2"; shift 2 ;;
    --no-jdk) NO_JDK=1; shift ;;
    --thread) THREAD_NAME="$2"; shift 2 ;;
    --all-threads) ALL_THREADS=1; shift ;;
    -h|--help)
      sed -n '1,40p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

# Locate jfr CLI
JFR_BIN=""
for cand in /Library/Java/JavaVirtualMachines/amazon-corretto-21.jdk/Contents/Home/bin/jfr \
            /Users/lihaoyi/Library/Java/JavaVirtualMachines/jbr-21.0.9/Contents/Home/bin/jfr; do
  if [[ -x "$cand" ]]; then JFR_BIN="$cand"; break; fi
done
if [[ -z "$JFR_BIN" ]]; then
  if command -v jfr >/dev/null 2>&1; then JFR_BIN="$(command -v jfr)"; fi
fi
if [[ -z "$JFR_BIN" ]]; then
  echo "ERROR: jfr CLI not found. Install JDK 21 or set PATH." >&2
  exit 1
fi

if [[ ! -f "$JFR_FILE" ]]; then
  echo "ERROR: JFR file not found: $JFR_FILE" >&2
  exit 1
fi

TMP_TXT="$(mktemp -t jfr-text.XXXXXX)"
trap 'rm -f "$TMP_TXT"' EXIT

echo "[analyze-jfr] reading $JFR_FILE..."
"$JFR_BIN" print --events jdk.ExecutionSample --stack-depth 64 "$JFR_FILE" > "$TMP_TXT"

python3 - "$TMP_TXT" "$TOP" "$SKIP_SEC" "$FILTER" "$NO_JDK" "$THREAD_NAME" "$ALL_THREADS" <<'PYEOF'
import re, sys, collections

txt_path, top_s, skip_s, filt, no_jdk_s, thread_name, all_threads_s = sys.argv[1:]
TOP = int(top_s)
SKIP_SEC = float(skip_s)
FILTER = filt
NO_JDK = no_jdk_s == "1"
THREAD = thread_name
ALL_THREADS = all_threads_s == "1"

text = open(txt_path).read()
# Split on event boundaries — each ExecutionSample begins with "jdk.ExecutionSample {"
events = text.split("jdk.ExecutionSample {")
# events[0] is preamble, skip
events = events[1:]

own = collections.Counter()       # method -> count when at top
total = collections.Counter()     # method -> count when anywhere
samples_kept = 0
samples_dropped_thread = 0
samples_dropped_warmup = 0

# Try to determine the earliest startTime to anchor relative seconds
ts_re = re.compile(r"startTime\s*=\s*(\d{2}):(\d{2}):(\d{2})\.(\d+)")
# Find first timestamp
first_t = None
for m in ts_re.finditer(text):
    h, mn, s, ms = m.groups()
    first_t = int(h)*3600 + int(mn)*60 + int(s) + int(ms)/1000.0
    break

# Frame line example:
#     dotty.tools.dotc.core.Types$Type.member(...) line: 123
# or  java.lang.ClassLoader.defineClass1(...) (no line)
# We strip params and lines, keeping fully qualified Class.method
frame_re = re.compile(r"^\s{4}(\S+)\(")  # method-ish frames inside stackTrace = [ ... ]

def normalize(method_str: str) -> str:
    # method_str is e.g. "dotty.tools.dotc.core.Types$Type.member"
    # ClassLoader.defineClass1 — ignore
    return method_str

for ev in events:
    # Extract thread name
    m_thr = re.search(r'sampledThread\s*=\s*"([^"]+)"', ev)
    thr = m_thr.group(1) if m_thr else ""

    if (not ALL_THREADS) and thr != THREAD:
        samples_dropped_thread += 1
        continue

    # Extract timestamp
    m_ts = ts_re.search(ev)
    if m_ts and first_t is not None:
        h, mn, s, ms = m_ts.groups()
        t = int(h)*3600 + int(mn)*60 + int(s) + int(ms)/1000.0
        if t - first_t < SKIP_SEC:
            samples_dropped_warmup += 1
            continue

    # Extract stack frames
    m_st = re.search(r'stackTrace\s*=\s*\[(.*?)\]\s*\}', ev, re.S)
    if not m_st:
        continue
    stack_block = m_st.group(1)
    frames = []
    for line in stack_block.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        # Frame format: "    pkg.Class.method(args) line: N" — strip leading whitespace
        s = line.strip()
        # Take the part before "(" — that's pkg.Class.method
        idx = s.find("(")
        if idx == -1:
            continue
        method = s[:idx]
        if NO_JDK and (method.startswith("java.") or method.startswith("jdk.internal.")
                       or method.startswith("sun.") or method.startswith("com.sun.")
                       or method.startswith("scala.collection.")
                       or method.startswith("scala.runtime.")
                       or method.startswith("scala.Function")
                       or method.startswith("scala.Tuple")):
            continue
        if FILTER and FILTER not in method:
            continue
        frames.append(method)

    if not frames:
        continue

    samples_kept += 1
    own[frames[0]] += 1
    seen = set()
    for f in frames:
        if f in seen:
            continue
        seen.add(f)
        total[f] += 1

print(f"\n=== JFR analysis ===")
print(f"Total samples kept: {samples_kept}")
print(f"  dropped (thread != {THREAD}): {samples_dropped_thread}")
print(f"  dropped (warmup < {SKIP_SEC}s): {samples_dropped_warmup}")
print(f"  filter: {FILTER or '(none)'}, no_jdk: {NO_JDK}, all_threads: {ALL_THREADS}")
print()

if samples_kept == 0:
    print("(no samples kept)")
    sys.exit(0)

def fmt_table(title, counter):
    print(title)
    print(f"  {'%':>6} {'count':>7}  method")
    for method, n in counter.most_common(TOP):
        pct = 100.0 * n / samples_kept
        print(f"  {pct:6.2f} {n:7d}  {method}")
    print()

fmt_table("=== Top by SELF time (sample at top of stack) ===", own)
fmt_table("=== Top by TOTAL/INCLUSIVE time (sample anywhere in stack) ===", total)
PYEOF
