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
| `analyze-jfr.py` | aggregate hot methods from a JFR file |
| `_common.py` | shared paths and subprocess helpers |

Generated state (gitignored) lives under `target/bench-mill-javalib/`:
`inputs/` (text files), `sources/` (extracted .scala/.java), `target/build/`
(JSON, logs, JFR).

## Usage

```
./setup-bench.py                # one time, populates target/bench-mill-javalib/{inputs,sources}
./quick-bench.py NAME           # short single-config run, prints SD/CI/iters
./run-bench.py [--warmup N ...] # full JMH run, JSON only
```
