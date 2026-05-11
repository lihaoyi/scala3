#!/usr/bin/env python3
"""Aggregate `jdk.ExecutionSample` events from a JFR file. Prints:
  - flat tables of top hot methods by self time and inclusive time
  - top-down call tree rooted at <root>, child = next inward stack frame
  - bottom-up reverse forest rooted at each top-N self-timed method,
    child = next outward stack frame (caller).

Both trees are pruned by a percent-of-samples threshold so the output
stays scannable. Use `--tree-threshold` (default 1.0%) and
`--reverse-threshold` (default 10% of the leaf method's samples) to
widen or tighten the views.
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


class Node:
    __slots__ = ("name", "total", "self_count", "children")
    def __init__(self, name: str):
        self.name = name
        self.total = 0
        self.self_count = 0
        self.children: dict[str, Node] = {}


def build_top_down(stacks: list[list[str]]) -> Node:
    """Top-down tree: root → outermost frame → ... → innermost.

    `total` counts stacks passing through; `self_count` counts stacks whose
    innermost frame IS this node.
    """
    root = Node("<root>")
    for frames in stacks:
        if not frames:
            continue
        cur = root
        cur.total += 1
        for f in reversed(frames):
            child = cur.children.get(f)
            if child is None:
                child = Node(f)
                cur.children[f] = child
            cur = child
            cur.total += 1
        cur.self_count += 1
    return root


def build_bottom_up(stacks: list[list[str]], target: str) -> Node:
    """Bottom-up tree rooted at `target`. Children are immediate callers
    (the next-outermost frame), then their callers, etc.

    `total` on root = number of stacks where target is the innermost frame.
    On a caller-node, `total` = number of those stacks routed through this
    caller chain.
    """
    root = Node(target)
    for frames in stacks:
        if not frames or frames[0] != target:
            continue
        cur = root
        cur.total += 1
        cur.self_count += 1
        for f in frames[1:]:
            child = cur.children.get(f)
            if child is None:
                child = Node(f)
                cur.children[f] = child
            cur = child
            cur.total += 1
    return root


def print_top_down(root: Node, kept: int, threshold_pct: float, max_depth: int) -> None:
    """Walk the top-down tree depth-first, sorted by `total` desc.

    Each line: `total% [self self%]  +-- method`. Pruned by threshold and depth.
    """
    if kept == 0:
        return
    threshold = threshold_pct * kept / 100.0
    print(f"\n=== Top-down call tree (>= {threshold_pct:.2f}% of {kept} samples) ===")
    print(f"  {'tot%':>6} {'self%':>6}  call tree")

    def walk(node: Node, depth: int) -> None:
        if depth > max_depth:
            return
        kids = sorted(node.children.values(), key=lambda n: -n.total)
        for kid in kids:
            if kid.total < threshold:
                continue
            tot = 100 * kid.total / kept
            slf = 100 * kid.self_count / kept
            indent = "  " * depth
            print(f"  {tot:6.2f} {slf:6.2f}  {indent}+ {kid.name}")
            walk(kid, depth + 1)

    walk(root, 0)


def print_bottom_up(stacks: list[list[str]], own: collections.Counter[str],
                    kept: int, top_n: int, threshold_pct: float, max_depth: int) -> None:
    """For each of top-N self-timed methods, print a reverse caller tree.

    `threshold_pct` is interpreted as "% of the leaf method's samples"
    (a node-local threshold), not % of total samples. This keeps each
    sub-forest scannable independently.
    """
    if kept == 0 or top_n <= 0:
        return
    print(f"\n=== Bottom-up reverse forest (top {top_n} self methods, "
          f"caller threshold >= {threshold_pct:.2f}% of leaf samples) ===")
    print(f"  {'self%':>6} {'leaf%':>6}  caller tree")

    for method, leaf_count in own.most_common(top_n):
        root = build_bottom_up(stacks, method)
        leaf_pct = 100 * leaf_count / kept
        print(f"\n  --- {method}  [self {leaf_pct:.2f}% of all samples, {leaf_count} samples]")
        threshold = threshold_pct * leaf_count / 100.0

        def walk(node: Node, depth: int) -> None:
            if depth > max_depth:
                return
            kids = sorted(node.children.values(), key=lambda n: -n.total)
            for kid in kids:
                if kid.total < threshold:
                    continue
                slf = 100 * kid.total / kept                # % of all samples
                leaf = 100 * kid.total / leaf_count          # % of leaf's samples
                indent = "  " * depth
                print(f"  {slf:6.2f} {leaf:6.2f}  {indent}^ {kid.name}")
                walk(kid, depth + 1)
        walk(root, 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jfr", type=Path, default=DEFAULT_JFR)
    ap.add_argument("--top", type=int, default=100,
                    help="Flat table row count (default 100).")
    ap.add_argument("--skip-warmup-sec", type=float, default=0.0)
    ap.add_argument("--filter", default="")
    ap.add_argument("--no-jdk", action="store_true")
    ap.add_argument("--thread", default="main")
    ap.add_argument("--all-threads", action="store_true")
    ap.add_argument("--tree", action=argparse.BooleanOptionalAction, default=True,
                    help="Print top-down call tree (default on).")
    ap.add_argument("--tree-threshold", type=float, default=1.0,
                    help="Top-down tree: min %% of total samples to keep a "
                         "node (default 1.0).")
    ap.add_argument("--tree-depth", type=int, default=40,
                    help="Top-down tree: max depth (default 40).")
    ap.add_argument("--reverse", action=argparse.BooleanOptionalAction, default=True,
                    help="Print bottom-up reverse forest (default on).")
    ap.add_argument("--reverse-top", type=int, default=10,
                    help="Bottom-up forest: include reverse tree for the "
                         "top-N self-time methods (default 10).")
    ap.add_argument("--reverse-threshold", type=float, default=10.0,
                    help="Bottom-up forest: min %% of the leaf method's "
                         "samples to keep a caller node (default 10.0).")
    ap.add_argument("--reverse-depth", type=int, default=20,
                    help="Bottom-up forest: max depth (default 20).")
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

    # Intern method strings to keep stack memory bounded even at 20k+ samples.
    pool: dict[str, str] = {}
    def intern(s: str) -> str:
        v = pool.get(s)
        if v is None:
            pool[s] = s
            return s
        return v

    own: collections.Counter[str] = collections.Counter()
    total: collections.Counter[str] = collections.Counter()
    stacks: list[list[str]] = []
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
            frames.append(intern(method))
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
        stacks.append(frames)

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

    if args.tree:
        root = build_top_down(stacks)
        print_top_down(root, kept, args.tree_threshold, args.tree_depth)
    if args.reverse:
        print_bottom_up(stacks, own, kept, args.reverse_top,
                        args.reverse_threshold, args.reverse_depth)


if __name__ == "__main__":
    main()
