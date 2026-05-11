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
                 --skip-warmup-sec 25 --top 50 \
                 --tree-threshold 1.0 \
                 --reverse-top 10 --reverse-threshold 10.0
```

`analyze-jfr.py` prints four sections:
1. **Top by SELF time** — flat table of hot leaf methods.
2. **Top by TOTAL/INCLUSIVE time** — flat table of methods that appear
   anywhere in the stack.
3. **Top-down call tree** — every node shows `tot%` (samples passing
   through) and `self%` (samples ending here). Nodes below
   `--tree-threshold` (% of total samples) or beyond `--tree-depth` are
   pruned. Frames are tagged with their source-file line number
   (`method:line`) by default; disable with `--no-with-lines`.
4. **Bottom-up reverse forest** — for each of the top
   `--reverse-top` self-timed methods, a tree of callers walking
   outward. Columns:
   - `tot%` = % of all samples whose stack reaches the leaf via this
     caller chain (same denominator as top-down's `tot%`).
   - `leaf%` = % of the leaf method's own self samples that came in
     through this caller path.

   `--reverse-threshold` is interpreted as % of the leaf method's own
   samples (node-local), so each sub-forest is independently scannable.

Use `--no-tree` / `--no-reverse` to suppress either tree if only the
flat tables are wanted.

**Stack depth.** `profile.py` records JFR with
`-XX:FlightRecorderOptions=stackdepth=1024` because the Scala compiler
routinely produces stacks deeper than JFR's default 64-frame cap
(recursive `Typer` + `InlineTyper`). Without this, the top-down tree
breaks into many disjoint mid-stack "roots" because outer frames are
truncated at recording time. `analyze-jfr.py` likewise reads with
`--stack-depth 1024`.
