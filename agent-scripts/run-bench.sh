#!/usr/bin/env bash
#
# run-bench.sh — Build the local non-bootstrapped scala3-compiler, then
# compile + run the bench-mill-javalib JMH benchmark using it.
#
# Reusable: re-run after editing scala3 sources to re-measure the local
# compiler's performance.
#
# Flags:
#   --verify         Quick run (1 warmup + 1 measurement) to confirm the
#                    setup compiles Mill cleanly. Fast but not a real number.
#   --skip-build     Skip the sbt rebuild of scala3-compiler-nonbootstrapped.
#   --warmup N       Override warmup iterations (default: 5).
#   --iterations N   Override measurement iterations (default: 5).
#   --time SECS      Per-iteration measurement time in seconds (default: 10).
#   --warmup-time S  Per-iteration warmup time in seconds (default: 5).
#   --forks N        Number of JVM forks (default: 1).
#   --                Pass remaining args directly to JMH.

set -euo pipefail

SCALA3_DIR="/Users/lihaoyi/Github/scala3"
BENCH_DIR="${SCALA3_DIR}/bench-mill-javalib"
INPUTS_DIR="${BENCH_DIR}/inputs"
SRC_DIR="${BENCH_DIR}/src"
BUILD_DIR="${BENCH_DIR}/build"
CLASSES_DIR="${BUILD_DIR}/classes"
GENERATED_DIR="${BUILD_DIR}/generated"
GENERATED_CLASSES_DIR="${BUILD_DIR}/generated-classes"

VERIFY_MODE=0
SKIP_BUILD=0
WARMUP=5
ITERATIONS=5
TIME=10
WARMUP_TIME=5
FORKS=1
EXTRA_JMH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) VERIFY_MODE=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --warmup) WARMUP="$2"; shift 2 ;;
    --iterations) ITERATIONS="$2"; shift 2 ;;
    --time) TIME="$2"; shift 2 ;;
    --warmup-time) WARMUP_TIME="$2"; shift 2 ;;
    --forks) FORKS="$2"; shift 2 ;;
    --) shift; EXTRA_JMH_ARGS=("$@"); break ;;
    *) EXTRA_JMH_ARGS+=("$1"); shift ;;
  esac
done

if [[ "$VERIFY_MODE" == "1" ]]; then
  WARMUP=1
  ITERATIONS=1
  TIME=5
  WARMUP_TIME=5
fi

echo "[run-bench] mode: $([[ $VERIFY_MODE == 1 ]] && echo verify || echo measure) (warmup=$WARMUP iters=$ITERATIONS time=${TIME}s warmupTime=${WARMUP_TIME}s forks=$FORKS)"

# 1. Make sure the local non-bootstrapped scala3-compiler classes are up to date.
if [[ "$SKIP_BUILD" == "0" ]]; then
  echo "[run-bench] (1/5) Building local non-bootstrapped scala3-compiler via sbt..."
  ( cd "${SCALA3_DIR}" && sbt --error 'scala3-compiler-nonbootstrapped/compile' )
else
  echo "[run-bench] (1/5) --skip-build set, NOT rebuilding scala3-compiler-nonbootstrapped"
fi

# 2. Export the full local non-bootstrapped scala3-compiler classpath.
#    This is the "compiler classpath" — what we put on the JVM classpath when
#    running the benchmark so dotty.tools.dotc.Driver is the LOCAL one.
echo "[run-bench] (2/5) Exporting local scala3-compiler-nonbootstrapped classpath..."
COMPILER_CP_FILE="${BUILD_DIR}/local-scala3-compiler.classpath"
mkdir -p "${BUILD_DIR}"
( cd "${SCALA3_DIR}" && sbt --error 'export scala3-compiler-nonbootstrapped/Compile/fullClasspath' ) > "${COMPILER_CP_FILE}"

# Drop classpath entries that don't exist on disk (e.g. the empty
# scala3-library-nonbootstrapped/classes directory). dotty's Classpath
# scanning is fine with missing entries but we may as well be tidy.
LOCAL_COMPILER_CP="$(tr ':' '\n' < "${COMPILER_CP_FILE}" | awk 'NF{print}' | while IFS= read -r p; do
  [[ -e "$p" ]] && echo "$p"
done | paste -sd: -)"

if [[ -z "${LOCAL_COMPILER_CP}" ]]; then
  echo "[run-bench] ERROR: empty local compiler classpath. Check sbt output:" >&2
  cat "${COMPILER_CP_FILE}" >&2
  exit 1
fi
echo "[run-bench]   local compiler cp entries: $(echo "${LOCAL_COMPILER_CP}" | tr ':' '\n' | wc -l | tr -d ' ')"

# 3. Resolve JMH jars (build-time generator + runtime core).
echo "[run-bench] (3/5) Resolving JMH..."
JMH_VERSION="1.37"
JMH_RUNTIME_CP="$(cs fetch -p \
  org.openjdk.jmh:jmh-core:${JMH_VERSION} \
  net.sf.jopt-simple:jopt-simple:5.0.4 \
  org.apache.commons:commons-math3:3.6.1)"
JMH_GEN_CP="$(cs fetch -p \
  org.openjdk.jmh:jmh-generator-bytecode:${JMH_VERSION} \
  org.openjdk.jmh:jmh-generator-asm:${JMH_VERSION} \
  org.openjdk.jmh:jmh-generator-reflection:${JMH_VERSION} \
  org.openjdk.jmh:jmh-core:${JMH_VERSION} \
  net.sf.jopt-simple:jopt-simple:5.0.4 \
  org.apache.commons:commons-math3:3.6.1 \
  org.ow2.asm:asm:9.7)"
# Force a newer ASM to the front of the JMH generator classpath. JMH ships
# with org.ow2.asm 9.0 which only understands class file major version <= 60
# (Java 16). Scala 3 emits Java 17+ bytecode by default. ASM 9.7 supports
# up to major version 65 (Java 21) which is plenty.
JMH_GEN_CP="$(cs fetch -p org.ow2.asm:asm:9.7):${JMH_GEN_CP}"

# 4. Compile the benchmark .scala using the LOCAL non-bootstrapped scala3-compiler.
echo "[run-bench] (4/5) Compiling benchmark sources with local scala3-compiler..."
rm -rf "${CLASSES_DIR}" "${GENERATED_DIR}" "${GENERATED_CLASSES_DIR}"
mkdir -p "${CLASSES_DIR}" "${GENERATED_DIR}" "${GENERATED_CLASSES_DIR}"

# When running dotc programmatically, the user-provided classpath ends up on the
# compile classpath. We need access to JMH's @Benchmark annotation and our local
# scala3-compiler API. The compiler itself loads from the same JVM classpath.
COMPILE_CP="${LOCAL_COMPILER_CP}:${JMH_RUNTIME_CP}"

mapfile -t BENCH_SOURCES < <(find "${SRC_DIR}" -name '*.scala' | sort)
if [[ ${#BENCH_SOURCES[@]} -eq 0 ]]; then
  echo "[run-bench] ERROR: no .scala sources in ${SRC_DIR}" >&2; exit 1
fi

java \
  -cp "${LOCAL_COMPILER_CP}" \
  dotty.tools.dotc.Main \
  -classpath "${COMPILE_CP}" \
  -d "${CLASSES_DIR}" \
  "${BENCH_SOURCES[@]}"

# 5. Run JMH bytecode generator over the compiled benchmark classes.
#    Use the "asm" generator because the "reflection" default loads the
#    compiled benchmark classes — which extend dotty.tools.dotc.Driver and
#    therefore would require the local compiler on the JVM classpath here too.
#    The asm generator just parses bytecode, no class loading needed.
echo "[run-bench]    Running JmhBytecodeGenerator (asm mode)..."
java \
  -cp "${JMH_GEN_CP}:${CLASSES_DIR}:${LOCAL_COMPILER_CP}" \
  org.openjdk.jmh.generators.bytecode.JmhBytecodeGenerator \
  "${CLASSES_DIR}" "${GENERATED_CLASSES_DIR}" "${GENERATED_DIR}" asm

# Step 5b: copy JMH-generated resources (META-INF/BenchmarkList etc.) and
# compile the generated .java stubs into the classes dir so org.openjdk.jmh.Main
# can discover and invoke the benchmarks.
if [[ -d "${GENERATED_DIR}/META-INF" ]]; then
  cp -R "${GENERATED_DIR}/META-INF" "${CLASSES_DIR}/"
fi
# Compile generated .java JMH stubs against (jmh-core + our compiled benchmark
# + the local scala3-compiler classes — the latter so the references to
# dotty.tools.dotc.Driver inside the @Benchmark superclass resolve).
mapfile -t JMH_STUBS < <(find "${GENERATED_CLASSES_DIR}" -name '*.java' 2>/dev/null | sort)
if [[ ${#JMH_STUBS[@]} -gt 0 ]]; then
  echo "[run-bench]    Compiling ${#JMH_STUBS[@]} JMH stub(s) with javac..."
  javac \
    -d "${CLASSES_DIR}" \
    -cp "${CLASSES_DIR}:${JMH_RUNTIME_CP}:${LOCAL_COMPILER_CP}" \
    "${JMH_STUBS[@]}"
fi

# 6. Run JMH.
echo "[run-bench] (5/5) Running JMH..."
RUN_CP="${CLASSES_DIR}:${LOCAL_COMPILER_CP}:${JMH_RUNTIME_CP}"

JMH_ARGS=(
  -wi "${WARMUP}"
  -i "${ITERATIONS}"
  -w "${WARMUP_TIME}s"
  -r "${TIME}s"
  -f "${FORKS}"
  -bm AverageTime
  -tu ms
  "${EXTRA_JMH_ARGS[@]}"
)

java \
  -Xms2g -Xmx4g \
  -Dbench.dir="${BENCH_DIR}" \
  -cp "${RUN_CP}" \
  org.openjdk.jmh.Main \
  "${JMH_ARGS[@]}"

echo "[run-bench] Done."
