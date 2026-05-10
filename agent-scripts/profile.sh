#!/usr/bin/env bash
#
# profile.sh — Profile a clean compile of the Mill libs.javalib corpus
# using the LOCAL non-bootstrapped scala3-compiler. Runs N compiles in
# a single JVM; JFR records the whole run. To get a clean post-warmup
# profile, do --runs 3 (or more) and the first 1-2 runs warm the JIT.
#
# Re-uses the classpath / source list / scalac options that run-bench.sh
# already prepared under bench-mill-javalib/inputs/. Does NOT depend on
# JMH; just runs dotty.tools.dotc.Main directly in a small Scala wrapper.
#
# Flags:
#   --skip-build         Reuse the previously sbt-built compiler classpath.
#   --vprofile           Add -Vprofile (compiler source/method complexity report).
#   --ystats             Add -Ystats (compiler internal stats; needs a Stats.enabled rebuild).
#   --yprofile           Add -Yprofile-enabled and write per-phase wall/CPU/alloc to a CSV.
#   --yprofile-out FILE  Override default CSV (bench-mill-javalib/build/profile/yprofile.csv).
#   --no-jfr             Disable JFR.
#   --jfr-out FILE       JFR output file (default: bench-mill-javalib/build/profile/profile.jfr).
#   --runs N             Number of compiles in a single JVM (default 3).
#   -- ...               Anything after `--` is forwarded as extra dotc args.

set -euo pipefail

SCALA3_DIR="/Users/lihaoyi/Github/scala3"
BENCH_DIR="${SCALA3_DIR}/bench-mill-javalib"
INPUTS_DIR="${BENCH_DIR}/inputs"
BUILD_DIR="${BENCH_DIR}/build"
PROFILE_DIR="${BUILD_DIR}/profile"
mkdir -p "${PROFILE_DIR}"

SKIP_BUILD=0
VPROFILE=0
YSTATS=0
YPROFILE=0
USE_JFR=1
JFR_OUT="${PROFILE_DIR}/profile.jfr"
YPROFILE_OUT="${PROFILE_DIR}/yprofile.csv"
RUNS=3
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    --vprofile) VPROFILE=1; shift ;;
    --ystats) YSTATS=1; shift ;;
    --yprofile) YPROFILE=1; shift ;;
    --yprofile-out) YPROFILE_OUT="$2"; shift 2 ;;
    --no-jfr) USE_JFR=0; shift ;;
    --jfr-out) JFR_OUT="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --) shift; PASSTHROUGH_ARGS=("$@"); break ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ "$SKIP_BUILD" == "0" ]]; then
  echo "[profile] Building scala3-compiler-nonbootstrapped..."
  ( cd "${SCALA3_DIR}" && sbt --error 'scala3-compiler-nonbootstrapped/compile' )
fi

COMPILER_CP_FILE="${BUILD_DIR}/local-scala3-compiler.classpath"
if [[ ! -s "${COMPILER_CP_FILE}" ]]; then
  echo "[profile] Exporting compiler classpath..."
  ( cd "${SCALA3_DIR}" && sbt --error 'export scala3-compiler-nonbootstrapped/Compile/fullClasspath' ) > "${COMPILER_CP_FILE}"
fi

LOCAL_COMPILER_CP="$(tr ':' '\n' < "${COMPILER_CP_FILE}" | awk 'NF{print}' | while IFS= read -r p; do
  [[ -e "$p" ]] && echo "$p"
done | paste -sd: -)"

# Compile a tiny driver wrapper that calls dotty.tools.dotc.Main.process N times.
DRIVER_SRC="${PROFILE_DIR}/ProfileRunner.scala"
DRIVER_CLASSES="${PROFILE_DIR}/classes"
mkdir -p "${DRIVER_CLASSES}"
cat > "${DRIVER_SRC}" <<'EOF'
package dotty.tools.benchmarks.profile

import java.io.File
import java.nio.file.{Files, Paths, Path}
import scala.io.Source

import dotty.tools.dotc.{Driver, Compiler, report}
import dotty.tools.dotc.core.Contexts.Context
import dotty.tools.dotc.reporting.{Reporter, StoreReporter}
import dotty.tools.io.AbstractFile
import dotty.tools.FatalError

/** Wrapper that mirrors MillJavalibBenchmark.SilentDriver but ALSO calls
 *  run.printSummary() so that -Vprofile / -Ystats output gets emitted.
 *  We intentionally skip the standard Driver.finish() path because that
 *  recurses on suspendedUnits and (in our setup) ends up surfacing
 *  spurious "is the name of value ... in <no file>" cyclic errors after
 *  plugin macros run. The Mill bench corpus has no real errors. */
class SilentDriver extends Driver {
  override def doCompile(compiler: Compiler, files: List[AbstractFile])(using ctx: Context): Reporter =
    if (files.nonEmpty)
      try {
        val run = compiler.newRun
        run.compile(files)
        run.printSummary()
        ctx.reporter
      } catch {
        case ex: FatalError =>
          report.error(ex.getMessage)
          ctx.reporter
      }
    else ctx.reporter
}

object ProfileRunner {
  def main(rawArgs: Array[String]): Unit = {
    val runs = sys.env.getOrElse("PROFILE_RUNS", "3").toInt
    val benchDir = sys.env.getOrElse("BENCH_DIR", "/Users/lihaoyi/Github/scala3/bench-mill-javalib")
    val inputs = Paths.get(benchDir, "inputs")
    val sources = Paths.get(benchDir, "sources")

    def lines(p: Path): List[String] = {
      val src = Source.fromFile(p.toFile)
      try src.getLines().toList.filter(_.trim.nonEmpty).map(_.trim)
      finally src.close()
    }

    val cp     = lines(inputs.resolve("compile-classpath.txt")).mkString(File.pathSeparator)
    val opts   = lines(inputs.resolve("scalac-options.txt"))
    val srcRel = lines(inputs.resolve("source-files.txt"))
    val srcAbs = srcRel.map(r => sources.resolve(r).toAbsolutePath.toString)

    val extraFromArgs = rawArgs.toList

    // Mirror MillJavalibBenchmark exactly: build a baseArgs once with -classpath
    // first, then opts, then extra; per-run only differ in -d <outDir> and the
    // source file list (so JIT can specialize on the same arg shape).
    val baseArgs: Array[String] =
      (List("-classpath", cp) ++ opts ++ extraFromArgs).toArray

    val sourceFiles: Array[String] = srcAbs.toArray

    // ONE driver re-used across runs (same as JMH benchmark — SilentDriver is
    // stateless, but reusing matches the bench shape).
    val driver = new SilentDriver

    // If the user passed flags that EMIT output we want to see (-Vprofile,
    // -Ystats, -Vphases, ...) use a ConsoleReporter so report.echo lands on
    // stdout. Otherwise StoreReporter for a quiet bench.
    val verboseFlagPrefixes = List("-Vprofile", "-Ystats", "-Vphases", "-Vprint", "-verbose")
    val verbose = extraFromArgs.exists(a => verboseFlagPrefixes.exists(a.startsWith))

    for (i <- 1 to runs) {
      val outDir = Files.createTempDirectory(s"scala3-profile-out-$i-")
      val args   = baseArgs ++ Array("-d", outDir.toAbsolutePath.toString) ++ sourceFiles
      val t0 = System.nanoTime()
      val reporter: Reporter =
        if verbose then new dotty.tools.dotc.reporting.ConsoleReporter()
        else new StoreReporter(null)
      val result = driver.process(args, reporter)
      val t1 = System.nanoTime()
      val ms = (t1 - t0) / 1000000L
      val errs = result.errorCount
      System.out.println(s"[profile-runner] run $i/$runs: $ms ms, errors=$errs")
      if (errs > 0) {
        val sample = result.allErrors.take(5)
        sample.foreach(d => System.out.println(s"[profile-runner]   ERR: ${d.position.orElse(null)}: ${d.message.linesIterator.take(2).mkString(" / ")}"))
      }
      // best-effort wipe
      try {
        val walk = Files.walk(outDir)
        try {
          val it = walk.iterator()
          val buf = scala.collection.mutable.ArrayBuffer[Path]()
          while (it.hasNext) buf += it.next()
          buf.reverse.foreach(p => try Files.deleteIfExists(p) catch { case _: Throwable => () })
        } finally walk.close()
      } catch { case _: Throwable => () }
    }
  }
}
EOF

if [[ ! -f "${DRIVER_CLASSES}/dotty/tools/benchmarks/profile/ProfileRunner.class" \
      || "${DRIVER_SRC}" -nt "${DRIVER_CLASSES}/dotty/tools/benchmarks/profile/ProfileRunner.class" ]]; then
  echo "[profile] Compiling ProfileRunner..."
  java -cp "${LOCAL_COMPILER_CP}" \
    dotty.tools.dotc.Main \
    -classpath "${LOCAL_COMPILER_CP}" \
    -d "${DRIVER_CLASSES}" \
    "${DRIVER_SRC}"
fi

EXTRA_OPTS=()
if [[ "$VPROFILE" == "1" ]]; then EXTRA_OPTS+=(-Vprofile); fi
if [[ "$YSTATS" == "1" ]]; then EXTRA_OPTS+=(-Ystats); fi
if [[ "$YPROFILE" == "1" ]]; then
  rm -f "${YPROFILE_OUT}"
  EXTRA_OPTS+=(-Yprofile-enabled -Yprofile-destination "${YPROFILE_OUT}")
fi
if [[ ${#PASSTHROUGH_ARGS[@]} -gt 0 ]]; then EXTRA_OPTS+=("${PASSTHROUGH_ARGS[@]}"); fi

JAVA_OPTS=( -Xms2g -Xmx4g )
if [[ "$USE_JFR" == "1" ]]; then
  rm -f "${JFR_OUT}"
  # 1ms execution sample period, profile settings.
  JAVA_OPTS+=( "-XX:StartFlightRecording=filename=${JFR_OUT},settings=profile,jdk.ExecutionSample#period=1ms,dumponexit=true" )
fi

echo "[profile] runs          = ${RUNS}"
echo "[profile] JFR output    = ${JFR_OUT} (enabled=${USE_JFR})"

PROFILE_RUNS=${RUNS} BENCH_DIR=${BENCH_DIR} \
java "${JAVA_OPTS[@]}" \
  -cp "${DRIVER_CLASSES}:${LOCAL_COMPILER_CP}" \
  dotty.tools.benchmarks.profile.ProfileRunner \
  "${EXTRA_OPTS[@]}"

echo "[profile] Done."
if [[ "$USE_JFR" == "1" ]]; then
  echo "[profile] JFR file: ${JFR_OUT}"
fi
