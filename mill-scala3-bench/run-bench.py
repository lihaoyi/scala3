#!/usr/bin/env python3
"""Build the local non-bootstrapped scala3-compiler, then compile and run
the MillJavalibBenchmark JMH benchmark using it.

Re-runnable. Use `--skip-build` after the compiler is already built.
"""
from __future__ import annotations
import argparse, os, shutil, sys
from pathlib import Path
from _common import (
    SCALA3_DIR, BENCH_DIR, SRC_DIR, BUILD_DIR, JMH_VERSION,
    run, sbt_export, existing_cp, coursier_fetch,
)

CLASSES_DIR = BUILD_DIR / "classes"
GENERATED_DIR = BUILD_DIR / "generated"
GENERATED_CLASSES_DIR = BUILD_DIR / "generated-classes"

JVM_FLAGS = [
    "-Xms4g", "-Xmx4g",
    "-XX:+AlwaysPreTouch",
    "-XX:+UseG1GC",
    "-XX:MaxGCPauseMillis=200",
    "-XX:-UseAdaptiveSizePolicy",
    "-XX:CICompilerCount=4",
    "-Djava.awt.headless=true",
]

def build_compiler():
    run(["sbt", "--error", "scala3-compiler-nonbootstrapped/compile"], cwd=SCALA3_DIR)

def compile_bench_sources(compiler_cp: str, jmh_runtime_cp: str) -> None:
    for d in (CLASSES_DIR, GENERATED_DIR, GENERATED_CLASSES_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    sources = sorted(p for p in SRC_DIR.rglob("*.scala"))
    if not sources:
        raise SystemExit(f"no .scala sources in {SRC_DIR}")
    run(
        ["java", "-cp", compiler_cp, "dotty.tools.dotc.Main",
         "-classpath", f"{compiler_cp}{os.pathsep}{jmh_runtime_cp}",
         "-d", str(CLASSES_DIR), *map(str, sources)]
    )

def jmh_generate(compiler_cp: str, jmh_gen_cp: str) -> None:
    run(
        ["java", "-cp", f"{jmh_gen_cp}{os.pathsep}{CLASSES_DIR}{os.pathsep}{compiler_cp}",
         "org.openjdk.jmh.generators.bytecode.JmhBytecodeGenerator",
         str(CLASSES_DIR), str(GENERATED_CLASSES_DIR), str(GENERATED_DIR), "asm"]
    )
    if (GENERATED_DIR / "META-INF").exists():
        shutil.copytree(GENERATED_DIR / "META-INF", CLASSES_DIR / "META-INF", dirs_exist_ok=True)
    stubs = sorted(p for p in GENERATED_CLASSES_DIR.rglob("*.java"))
    if stubs:
        run(
            ["javac", "-d", str(CLASSES_DIR),
             "-cp", f"{CLASSES_DIR}{os.pathsep}{jmh_gen_cp}{os.pathsep}{compiler_cp}",
             *map(str, stubs)]
        )

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--warmup", type=int, default=8)
    ap.add_argument("--warmup-time", type=int, default=5)
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--time", type=int, default=10)
    ap.add_argument("--forks", type=int, default=1)
    ap.add_argument("--jvm-extra", default="",
                    help="Extra JVM flags appended to JVM_FLAGS, space-separated.")
    ap.add_argument("jmh_extra", nargs=argparse.REMAINDER,
                    help="Anything after `--` is passed through to JMH.")
    args = ap.parse_args()
    jmh_passthru = args.jmh_extra[1:] if args.jmh_extra[:1] == ["--"] else args.jmh_extra

    if not args.skip_build:
        build_compiler()

    compiler_cp = existing_cp(sbt_export("scala3-compiler-nonbootstrapped/Compile/fullClasspath"))
    if not compiler_cp:
        raise SystemExit("empty compiler classpath")

    jmh_runtime_cp = coursier_fetch(
        f"org.openjdk.jmh:jmh-core:{JMH_VERSION}",
        "net.sf.jopt-simple:jopt-simple:5.0.4",
        "org.apache.commons:commons-math3:3.6.1",
    )
    # JMH ships ASM 9.0 which can't read JDK17+ bytecode; force 9.7 to the front.
    jmh_gen_cp = coursier_fetch("org.ow2.asm:asm:9.7") + os.pathsep + coursier_fetch(
        f"org.openjdk.jmh:jmh-generator-bytecode:{JMH_VERSION}",
        f"org.openjdk.jmh:jmh-generator-asm:{JMH_VERSION}",
        f"org.openjdk.jmh:jmh-generator-reflection:{JMH_VERSION}",
        f"org.openjdk.jmh:jmh-core:{JMH_VERSION}",
        "net.sf.jopt-simple:jopt-simple:5.0.4",
        "org.apache.commons:commons-math3:3.6.1",
        "org.ow2.asm:asm:9.7",
    )

    compile_bench_sources(compiler_cp, jmh_runtime_cp)
    jmh_generate(compiler_cp, jmh_gen_cp)

    run_cp = f"{CLASSES_DIR}{os.pathsep}{compiler_cp}{os.pathsep}{jmh_runtime_cp}"
    jvm_flags = list(JVM_FLAGS)
    if args.jvm_extra:
        jvm_flags.extend(args.jvm_extra.split())
    # Anchor the regex so MillJavalibBenchmark.compile is selected but not
    # MillJavalibBenchmarkAB.compile (the AB harness needs separate setup).
    bench_pattern = r"MillJavalibBenchmark\.compile$"
    run(
        ["java", *jvm_flags, f"-Dbench.dir={BENCH_DIR}",
         "-cp", run_cp, "org.openjdk.jmh.Main",
         "-wi", str(args.warmup),
         "-i", str(args.iterations),
         "-w", f"{args.warmup_time}s",
         "-r", f"{args.time}s",
         "-f", str(args.forks),
         "-bm", "AverageTime",
         "-tu", "ms",
         bench_pattern,
         *jmh_passthru]
    )

if __name__ == "__main__":
    main()
