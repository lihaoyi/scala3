#!/usr/bin/env python3
"""Profile a clean compile of the Mill libs.javalib corpus using the local
non-bootstrapped scala3-compiler. Runs N compiles in a single JVM with JFR
recording the whole run. Use --runs 3+ so the first 1-2 runs warm the JIT.
"""
from __future__ import annotations
import argparse, os, subprocess, sys
from pathlib import Path
from _common import (
    SCALA3_DIR, BENCH_DIR, BUILD_DIR, SCRIPT_DIR,
    run, sbt_export, existing_cp,
)

PROFILE_DIR = BUILD_DIR / "profile"
PROFILE_RUNNER = "dotty.tools.benchmarks.profile.ProfileRunner"

def compile_runner(compiler_cp: str) -> Path:
    classes = PROFILE_DIR / "classes"
    src = SCRIPT_DIR / "src" / "ProfileRunner.scala"
    target = classes / "dotty/tools/benchmarks/profile/ProfileRunner.class"
    if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime:
        return classes
    classes.mkdir(parents=True, exist_ok=True)
    run(["java", "-cp", compiler_cp, "dotty.tools.dotc.Main",
         "-classpath", compiler_cp, "-d", str(classes), str(src)])
    return classes

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--vprofile", action="store_true")
    ap.add_argument("--ystats", action="store_true")
    ap.add_argument("--yprofile", action="store_true")
    ap.add_argument("--yprofile-out", type=Path, default=PROFILE_DIR / "yprofile.csv")
    ap.add_argument("--no-jfr", dest="use_jfr", action="store_false")
    ap.add_argument("--jfr-out", type=Path, default=PROFILE_DIR / "profile.jfr")
    ap.add_argument("--runs", type=int, default=8,
                    help="Number of compile passes in one JVM. Higher = more JFR samples after "
                         "warmup. Default 8 yields ~5x the sample density of the prior default 3.")
    ap.add_argument("dotc_extra", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    extra = args.dotc_extra[1:] if args.dotc_extra[:1] == ["--"] else args.dotc_extra

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if not args.skip_build:
        run(["sbt", "--error", "scala3-compiler-nonbootstrapped/compile"], cwd=SCALA3_DIR)
    compiler_cp = existing_cp(sbt_export("scala3-compiler-nonbootstrapped/Compile/fullClasspath"))
    runner_classes = compile_runner(compiler_cp)

    java_opts = ["-Xms2g", "-Xmx4g"]
    if args.use_jfr:
        args.jfr_out.unlink(missing_ok=True)
        # `stackdepth=1024`: JFR's default 64-frame cap truncates Scala
        # compilation stacks (recursive Typer + InlineTyper routinely
        # blow past 80 frames), which makes the analyzer's top-down tree
        # show many disjoint mid-stack "roots". 1024 is overkill but
        # cheap enough that we leave headroom.
        java_opts.append("-XX:FlightRecorderOptions=stackdepth=1024")
        java_opts.append(
            f"-XX:StartFlightRecording=filename={args.jfr_out},settings=profile,"
            f"jdk.ExecutionSample#period=1ms,dumponexit=true"
        )
    dotc_opts: list[str] = []
    if args.vprofile: dotc_opts.append("-Vprofile")
    if args.ystats: dotc_opts.append("-Ystats")
    if args.yprofile:
        args.yprofile_out.unlink(missing_ok=True)
        dotc_opts += ["-Yprofile-enabled", "-Yprofile-destination", str(args.yprofile_out)]
    dotc_opts += extra

    print(f"[profile] runs={args.runs}  JFR={'on' if args.use_jfr else 'off'} -> {args.jfr_out if args.use_jfr else '-'}")
    env = {**os.environ, "PROFILE_RUNS": str(args.runs), "BENCH_DIR": str(BENCH_DIR)}
    run(
        ["java", *java_opts,
         "-cp", f"{runner_classes}{os.pathsep}{compiler_cp}",
         PROFILE_RUNNER, *dotc_opts],
        env=env,
    )

if __name__ == "__main__":
    main()
