# mill-scala3-bench

JMH benchmark that measures how long the local `scala3-compiler-nonbootstrapped`
takes to do a clean compile of Mill's `libs.javalib` module graph.

## Layout

| path | role |
|---|---|
| `src/MillJavalibBenchmark.scala` | the @Benchmark — one compile per invocation |
| `src/MillJavalibBenchmarkAB.scala` | paired A/B harness for two compiler builds |
| `src/ProfileRunner.scala` | non-JMH driver used by `profile.py` |
| `setup-bench.py` | fetches Mill source jars + classpath from Maven Central |
| `run-bench.py` | builds the local compiler, compiles + runs the bench |
| `quick-bench.py` | thin wrapper around `run-bench.py` that prints a stats summary |
| `profile.py` | run the bench under JFR / `-Yprofile-enabled` for hot-method analysis |
| `analyze-jmh.py` | post-process JMH JSON: bootstrap CI, drop-first, paired delta |
| `analyze-jfr.py` | aggregate hot methods from a JFR file (flat tables + top-down call tree + bottom-up reverse forest) |
| `_common.py` | shared paths and subprocess helpers |

Generated state (gitignored) lives under `target/bench-mill-javalib/`:
`inputs/` (text files), `sources/` (extracted .scala/.java), `target/build/`
(JSON, logs, JFR).

## Usage

```
./setup-bench.py                # one time, populates target/bench-mill-javalib/{inputs,sources}
./quick-bench.py NAME           # short single-config run, prints SD/CI/iters
./run-bench.py [--warmup N ...] # full JMH run, JSON only
./profile.py --runs 8 --jfr-out target/bench-mill-javalib/profile.jfr  # JFR profile
./analyze-jfr.py --jfr target/bench-mill-javalib/profile.jfr \
                 --skip-warmup-sec 25 \
                 --tree-threshold 1.0 \
                 --reverse-top 20 --reverse-threshold 10.0
```

The report is auto-saved to `<jfr-stem>-analyze.txt` alongside the JFR
(override with `--out PATH`, suppress with `--no-out`). `analyze-jfr.py`
prints two sections:

1. **Top-down call tree** — every node shows `tot%` (samples passing
   through) and `self%` (samples ending here). Nodes below
   `--tree-threshold` (% of total samples) or beyond `--tree-depth` are
   pruned. Frames are tagged with their source-file line number
   (`method:line`) by default; disable with `--no-with-lines`.
2. **Bottom-up reverse forest** — for each of the top
   `--reverse-top` self-timed methods, a tree of callers walking
   outward. The header line for each leaf shows its self-time count
   (and `% of all samples`), which subsumes the previous flat
   "top by self time" table.

   Columns (both inclusive, not self time):
   - `tot%` = % of **all profile samples** (denominator = every kept
     sample in the JFR) whose stack contains this caller-chain ending
     at the leaf. Same denominator as top-down's `tot%`.
   - `leaf%` = % of the **leaf method's own samples** (denominator =
     the leaf's sample count, shown in its header) that flowed through
     this caller path.

   Worked example: if a leaf reads
   `--- Objects.equals  [self 2.04% of all samples, 328 samples]`
   and a caller row reads `1.23  60.37  ^ SourceFile.equals:123`,
   that means **198 of the leaf's 328 samples** (60.37%) came in
   through `SourceFile.equals` — and those 198 samples are **1.23%
   of the whole profile** (1.23% × kept ≈ 198).

   `--reverse-threshold` (default 5%) keeps any caller whose share of
   the leaf method's samples is >= the threshold. The simple
   leaf-relative cutoff lets siblings at the same level survive (so
   branching shows up), while recursive chains self-terminate as their
   sample share decays below the threshold.

Use `--no-tree` / `--no-reverse` to suppress either tree.

**Stack depth.** `profile.py` records JFR with
`-XX:FlightRecorderOptions=stackdepth=1024` because the Scala compiler
routinely produces stacks deeper than JFR's default 64-frame cap
(recursive `Typer` + `InlineTyper`). Without this, the top-down tree
breaks into many disjoint mid-stack "roots" because outer frames are
truncated at recording time. `analyze-jfr.py` likewise reads with
`--stack-depth 1024`.
