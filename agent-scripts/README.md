# agent-scripts

Helper scripts for benchmarking the local Scala 3 compiler against
`com-lihaoyi/mill`'s `libs.javalib` module.

## Files

- **`setup-bench.sh`** — One-time setup. Walks the transitive Mill module
  graph rooted at `libs.javalib`, copies every `.scala`/`.java` source file
  into `bench-mill-javalib/sources/`, and captures the merged compile
  classpath and scalac options into `bench-mill-javalib/inputs/`. Re-run
  whenever Mill's module deps or sources change. Idempotent — overwrites
  previous output.

- **`run-bench.sh`** — Re-runnable. Rebuilds the local `scala3-compiler-nonbootstrapped`
  via sbt, exports its classpath, compiles the JMH benchmark using the
  freshly built compiler, runs the JMH bytecode generator, then runs JMH.

  Useful flags:
  - `--verify` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Single-warmup, single-measurement smoke run (~30s).
  - `--skip-build` &nbsp;Skip the sbt rebuild of the compiler (use when iterating).
  - `--warmup N`, `--iterations N` &nbsp;Override the iteration counts (default 5 / 5).
  - `--time SECS`, `--warmup-time SECS` &nbsp;Override per-iteration time (default 10s / 5s).
  - `--forks N` &nbsp;&nbsp;&nbsp;&nbsp;Override fork count (default 1).
  - Anything after `--` is forwarded to JMH's CLI.

- **`profile.sh`** — Re-runnable. Compiles the Mill libs.javalib corpus N
  times in a single JVM (default 3) using the local non-bootstrapped
  compiler. By default writes a JFR file with 1ms execution-sample period.
  Optionally also adds `-Vprofile`, `-Yprofile-enabled` (per-phase
  wall/CPU/alloc CSV), or any `-- ...` passthrough scalac flag.

  The script's runner uses a `SilentDriver` that calls `compiler.newRun`
  directly, mirroring the JMH benchmark, instead of `Driver.process` —
  the latter triggers `finish()` → `compileSuspendedUnits` recursion that
  produces ~3000 spurious cyclic-name errors on this corpus when plugin
  macros (`mill-moduledefs`, `unroll`) suspend units. Fixing the runner
  was step 1.0 of the perf loop.

  Useful flags:
  - `--skip-build` &nbsp;Reuse existing compiler classpath (avoids 30+s sbt step).
  - `--vprofile` &nbsp;Adds `-Vprofile` (per-source method/complexity table).
  - `--yprofile` &nbsp;Adds `-Yprofile-enabled` and writes phase wall/CPU/alloc CSV.
  - `--ystats` &nbsp;Adds `-Ystats` (only useful after rebuilding with `Stats.enabled = true`).
  - `--no-jfr` &nbsp;Disable JFR (default: enabled).
  - `--runs N` &nbsp;Number of compiles in the same JVM (default 3, JIT warms by run 2-3).
  - `--jfr-out FILE`, `--yprofile-out FILE` &nbsp;Override default output paths.
  - `-- ...` &nbsp;Anything after `--` is forwarded to dotc.

  Verbose flags (`-Vprofile`, `-Ystats`, `-Vphases`, `-Vprint`, `-verbose`)
  automatically switch the runner to `ConsoleReporter` so `report.echo`
  output reaches stdout. Otherwise we use a `StoreReporter` to keep the
  bench quiet.

- **`analyze-jfr.sh`** — Aggregates `jdk.ExecutionSample` events from a
  JFR file and prints top hot methods by self-time and total/inclusive
  time. Defaults: filter to thread `main`, drop `java.*` / `jdk.*` / scala
  collection plumbing frames. Useful flags:
  - `--skip-warmup-sec S` &nbsp;Drop the first S seconds of samples (use to
    skip the cold first compile in a `--runs 3` profile run).
  - `--filter STR` &nbsp;Keep only samples that touch a method name
    containing STR (e.g. `--filter Inliner`, `--filter TypeComparer`).
  - `--top N`, `--all-threads`, `--no-jdk` (default on).

## Outputs

`bench-mill-javalib/inputs/`

| file | purpose |
| --- | --- |
| `source-files.txt` | relative paths under `sources/` of every Mill source file |
| `compile-classpath.txt` | union of every transitive module's compile classpath, internal Mill output paths stripped |
| `scalac-options.txt` | scalac options used by Mill's `libs.javalib`, including `-Xplugin:` entries |
| `_*.json` | raw JSON dumps from `mill show` (not consumed by the benchmark) |

`bench-mill-javalib/build/`

Built by `run-bench.sh`. Wiped on each invocation.

`bench-mill-javalib/build/profile/` (created by `profile.sh`):

| file | purpose |
| --- | --- |
| `profile.jfr` | JFR recording (1ms exec sample period). Feed to `analyze-jfr.sh`. |
| `yprofile.csv` | `-Yprofile-enabled` output: per-phase wall_ns / cpu_ns / alloc_bytes / heap_bytes plus GC events. |
| `vprofile.log` | `-Vprofile` source-complexity table (when `--vprofile` is used). |
| `analyze.txt` | Latest `analyze-jfr.sh` text dump (saved by hand for now). |

## Quirks

- Mill's `libs.javalib` is built with **Scala 3.8.2**; the local compiler
  reports as `3.8.4-RC2-bin-SNAPSHOT-nonbootstrapped`. Cross-version is fine
  for the patch/RC level but the moduledefs and unroll plugins are
  TASTY-loaded from 3.8.2 jars; if scalac's plugin loader rejects them on
  some future bump, drop those `-Xplugin:` entries from `scalac-options.txt`.
- The benchmark forces `org.ow2.asm:9.7` onto the JMH bytecode generator
  classpath because the bundled ASM 9.0 cannot read class file major
  version 61 (Java 17) emitted by Scala 3 by default.
- Mill writes a few build-info `.scala/.java` files under
  `out/.../buildInfoSources.dest/`; those paths are also copied into
  `sources/out/...`. They are real source files and need to be on the
  compile list.
- One innocuous warning ("`mocking up superclass for module class internal`")
  is emitted from dotty's JVM backend on every iteration. It is benign and
  comes from a Mill marker module class.
