#!/usr/bin/env python3
"""Aggregate `jdk.ExecutionSample` events from a JFR file. Prints:
  - top-down call tree rooted at <root>, child = next inward stack frame
  - bottom-up reverse forest rooted at each top-N self-timed method,
    child = next outward stack frame (caller).

Both trees are pruned by a percent-of-samples threshold so the output
stays scannable. Use `--tree-threshold` (default 1.0%) and
`--reverse-threshold` (default 10% of the leaf method's samples) to
widen or tighten the views.

By default the report is also written to `<jfr-stem>-analyze.txt`
alongside the JFR file (use `--out PATH` to override or `--no-out` to
suppress).
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


def build_top_down(stacks: list[tuple[list[str], list[str]]]) -> Node:
    """Top-down tree built from `tree_frames` (line-tagged when enabled).
    `total` counts stacks passing through; `self_count` counts stacks whose
    innermost frame IS this node.
    """
    root = Node("<root>")
    for _, tree_frames in stacks:
        if not tree_frames:
            continue
        cur = root
        cur.total += 1
        for f in reversed(tree_frames):
            child = cur.children.get(f)
            if child is None:
                child = Node(f)
                cur.children[f] = child
            cur = child
            cur.total += 1
        cur.self_count += 1
    return root


def build_bottom_up(stacks: list[tuple[list[str], list[str]]], target: str) -> Node:
    """Bottom-up tree rooted at the bare method `target`. Children are
    immediate callers walking outward, tagged with their call-site lines
    when `--with-lines` is on.

    The root key matches against bare method names (so all source lines of
    the leaf method aggregate into one root). Caller frames use the
    line-tagged form to split branches per call site.
    """
    root = Node(target)
    for methods, tree_frames in stacks:
        if not methods or methods[0] != target:
            continue
        cur = root
        cur.total += 1
        cur.self_count += 1
        for f in tree_frames[1:]:
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


def print_bottom_up(stacks: list[tuple[list[str], list[str]]],
                    own: collections.Counter[str],
                    kept: int, top_n: int, leaf_pct_threshold: float,
                    max_depth: int) -> None:
    """For each of top-N self-timed methods, print a reverse caller tree.

    Columns:
      tot%  = % of all samples whose stack reaches the leaf via this
              caller chain (inclusive — same denominator as top-down's
              `tot%`).
      leaf% = % of the leaf method's OWN self samples that flowed
              through this caller path.

    A caller node is kept iff it accounts for >= `leaf_pct_threshold` of
    the leaf method's samples. This is the simplest filter and the most
    intuitive: any caller that's a meaningful fraction of the leaf's
    time shows up, including SIBLINGS at the same depth (so branching
    survives). Recursive chains naturally self-terminate because samples
    decay along the chain and eventually fall below the threshold.
    """
    if kept == 0 or top_n <= 0:
        return
    print(f"\n=== Bottom-up reverse forest (top {top_n} self methods, "
          f"caller >= {leaf_pct_threshold:.1f}% of leaf samples) ===")
    print(f"  Each `---` block has a leaf hotspot (marked `*`) followed by")
    print(f"  its caller chain (marked `^`, walking OUT toward main).")
    print(f"  Columns:")
    print(f"    tot%  = % of ALL profile samples whose stack contains")
    print(f"            this caller-chain ending at the leaf. The leaf's")
    print(f"            own row reports its self %.")
    print(f"    leaf% = % of the LEAF method's own samples that came in")
    print(f"            through this caller chain. Always 100% on the")
    print(f"            leaf row itself; sums of direct callers ≈ 100%")
    print(f"            modulo threshold-pruned siblings.\n")
    print(f"  {'tot%':>6} {'leaf%':>6}  call tree")

    for method, leaf_count in own.most_common(top_n):
        root = build_bottom_up(stacks, method)
        leaf_pct = 100 * leaf_count / kept
        floor = leaf_count * leaf_pct_threshold / 100.0
        # Print the leaf hotspot itself as a normal row (tot% = its share of
        # all samples, leaf% = 100% by definition). The `---` separator alone
        # delimits leaf sub-trees; callers below get the standard `^` prefix.
        print(f"\n  ---")
        print(f"  {leaf_pct:6.2f} {100.0:6.2f}  * {method}")

        def walk(node: Node, depth: int) -> None:
            if depth > max_depth:
                return
            kids = sorted(node.children.values(), key=lambda n: -n.total)
            for kid in kids:
                if kid.total < floor:
                    continue
                tot = 100 * kid.total / kept                 # % of all samples
                leaf = 100 * kid.total / leaf_count          # % of leaf's samples
                indent = "  " * depth
                print(f"  {tot:6.2f} {leaf:6.2f}  {indent}^ {kid.name}")
                walk(kid, depth + 1)
        walk(root, 0)


class Tee:
    """Forward writes to multiple streams. Used to keep the human-visible
    stdout output AND a persisted file in one pass — the analysis takes
    several seconds and shouldn't have to be re-run just to recover what
    scrolled past."""
    def __init__(self, *streams):
        self._streams = streams
    def write(self, s):
        for st in self._streams:
            st.write(s)
    def flush(self):
        for st in self._streams:
            st.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jfr", type=Path, default=DEFAULT_JFR)
    ap.add_argument("--out", type=Path, default=None,
                    help="Write a copy of the report to this path. Defaults "
                         "to `<jfr-stem>-analyze.txt` alongside the JFR. "
                         "Pass `--out -` (or `--no-out`) to skip the file.")
    ap.add_argument("--no-out", action="store_true",
                    help="Suppress the auto-saved report file; print only "
                         "to stdout.")
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
    ap.add_argument("--reverse-top", type=int, default=20,
                    help="Bottom-up forest: include reverse tree for the "
                         "top-N self-time methods (default 20).")
    ap.add_argument("--reverse-threshold", type=float, default=5.0,
                    help="Bottom-up forest: keep a caller node iff it "
                         "accounts for at least this %% of the leaf "
                         "method's own samples (default 5.0). Smaller "
                         "value = more branching + deeper chains; "
                         "larger value = tighter trees.")
    ap.add_argument("--reverse-depth", type=int, default=30,
                    help="Bottom-up forest: max depth (default 30).")
    ap.add_argument("--with-lines", action=argparse.BooleanOptionalAction, default=True,
                    help="Append JFR-reported line numbers to method names "
                         "in the tree views (default on). Aggregates "
                         "recursive calls per source line for clearer "
                         "branch attribution. Flat tables always use bare "
                         "method names.")
    args = ap.parse_args()
    if not args.jfr.is_file():
        sys.exit(f"JFR file not found: {args.jfr}")

    # Resolve the output file (None → auto, "-" / --no-out → skip).
    out_path: Path | None
    if args.no_out or (args.out is not None and str(args.out) == "-"):
        out_path = None
    elif args.out is not None:
        out_path = args.out
    else:
        out_path = args.jfr.with_name(args.jfr.stem + "-analyze.txt")

    out_file = None
    real_stdout = sys.stdout
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_file = out_path.open("w")
        sys.stdout = Tee(real_stdout, out_file)

    try:
        run_analysis(args)
    finally:
        if out_file is not None:
            sys.stdout = real_stdout
            out_file.close()
            print(f"[analyze-jfr] report saved to {out_path}")


def run_analysis(args):
    print(f"[analyze-jfr] reading {args.jfr} ...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        subprocess.run(
            [find_jfr_bin(), "print",
             "--events", "jdk.ExecutionSample",
             "--stack-depth", "1024", str(args.jfr)],
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

    # `own` ranks the leaf-method roots used by the bottom-up forest.
    own: collections.Counter[str] = collections.Counter()
    # Two parallel forms per sample: `methods` is bare method names (used by
    # bottom-up roots so leaves aggregate regardless of call-site line).
    # `tree_frames` includes `:line` when `--with-lines` is on, so tree
    # nodes split per source line — this disambiguates recursive callers
    # (e.g. `Type.dealias:1547` vs `Type.dealias:1559`).
    stacks: list[tuple[list[str], list[str]]] = []
    kept = dropped_thread = dropped_warmup = 0

    line_re = re.compile(r"\s+line:\s*(\d+)\s*$")

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
        methods: list[str] = []
        tree_frames: list[str] = []
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
            methods.append(intern(method))
            if args.with_lines:
                m_line = line_re.search(s)
                tag = f"{method}:{m_line.group(1)}" if m_line else method
                tree_frames.append(intern(tag))
            else:
                tree_frames.append(intern(method))
        if not methods:
            continue
        kept += 1
        own[methods[0]] += 1
        stacks.append((methods, tree_frames))

    print("\n=== JFR analysis ===")
    print(f"Total samples kept: {kept}")
    print(f"  dropped (thread != {args.thread}): {dropped_thread}")
    print(f"  dropped (warmup < {args.skip_warmup_sec}s): {dropped_warmup}")
    print(f"  filter: {args.filter or '(none)'}, no_jdk: {args.no_jdk}, all_threads: {args.all_threads}")
    if kept == 0:
        return

    if args.tree:
        root = build_top_down(stacks)
        print_top_down(root, kept, args.tree_threshold, args.tree_depth)
    if args.reverse:
        print_bottom_up(stacks, own, kept, args.reverse_top,
                        args.reverse_threshold, args.reverse_depth)


if __name__ == "__main__":
    main()
