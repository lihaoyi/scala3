#!/usr/bin/env python3
"""One-time setup: populate target/bench-mill-javalib/{sources,inputs}/ from
Mill's published Maven Central artifacts.

Downloads source jars for the Mill module graph rooted at `mill-libs`, extracts
them into `sources/<jar-stem>/...` (one subdirectory per source jar so files at
the same path in different jars do not overwrite each other — e.g. several Mill
modules ship top-level `ScalaModule.scala` files), resolves the binary compile
classpath with coursier, and writes `compile-classpath.txt`, `scalac-options.txt`,
and `source-files.txt` into `inputs/`. Coursier resolves the full transitive
closure of `mill-*` artifacts under `mill-libs`, so the corpus covers every
Mill module the umbrella pulls in (javalib, scalalib, kotlinlib, core-*, etc.).

Re-run when MILL_VERSION or MILL_ROOT_MODULE changes; idempotent otherwise.
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys, zipfile
from pathlib import Path
from _common import BENCH_DIR, INPUTS_DIR, SOURCES_DIR, run

# Root Mill umbrella module. Coursier walks its dependency graph and we recompile
# every transitive `mill-*` artifact from source. Published as `mill-libs_3`.
MILL_VERSION = "1.1.6"
MILL_ROOT_MODULE = "libs"

# These track the Scala binary version used by Mill at the chosen MILL_VERSION.
SCALA_PLUGIN_BINARY_VERSION = "3.8.2"
MILL_MODULEDEFS_VERSION = "0.13.1"

PLUGINS = [
    f"com.lihaoyi:scalac-mill-moduledefs-plugin_{SCALA_PLUGIN_BINARY_VERSION}:{MILL_MODULEDEFS_VERSION}",
    "com.lihaoyi:unroll-plugin_3:0.2.0",
]

# Mill's own build wires these into the compile classpath but doesn't declare
# them in published poms. Several files (e.g. mill-core-api's Shims.scala)
# `import dotty.tools.dotc.*`, so we need scala3-compiler at compile time.
EXTRA_COMPILE_DEPS = [
    f"org.scala-lang:scala3-compiler_3:{SCALA_PLUGIN_BINARY_VERSION}",
]
SCALAC_OPTIONS_TAIL = [
    "-deprecation",
    "-feature",
    "-Xkind-projector:underscores",
]

def mill_root_coord(version: str) -> str:
    return f"com.lihaoyi:mill-{MILL_ROOT_MODULE}_3:{version}"

def cs_fetch(coords: list[str], *, sources: bool = False) -> list[Path]:
    cmd = ["cs", "fetch", "-p", *coords]
    if sources:
        cmd.insert(2, "--sources")
    out = run(cmd, capture=True).stdout
    return [Path(p) for p in out.strip().split(os.pathsep) if p]

def is_mill_jar(p: Path) -> bool:
    return p.name.startswith("mill-") and p.name.endswith(".jar")

def jar_subdir_name(jar: Path) -> str:
    # mill-libs-scalalib_3-1.1.6-sources.jar -> mill-libs-scalalib_3-1.1.6
    name = jar.name
    if name.endswith("-sources.jar"):
        name = name[:-len("-sources.jar")]
    elif name.endswith(".jar"):
        name = name[:-len(".jar")]
    return name

def extract_sources(jars: list[Path], dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    count = 0
    for jar in jars:
        sub = dest / jar_subdir_name(jar)
        with zipfile.ZipFile(jar) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if not (info.filename.endswith(".scala") or info.filename.endswith(".java")):
                    continue
                target = sub / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    return count

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mill-version", default=MILL_VERSION,
                    help=f"Mill version on Maven Central (default {MILL_VERSION}).")
    args = ap.parse_args()
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    root_coord = mill_root_coord(args.mill_version)

    print(f"[setup-bench] Mill version: {args.mill_version}")
    print(f"[setup-bench] Root module:  {root_coord}")

    print("[setup-bench] Fetching Mill source jars (transitive) ...")
    source_jars = [j for j in cs_fetch([root_coord], sources=True)
                   if j.name.endswith("-sources.jar") and j.name.startswith("mill-")]
    print(f"[setup-bench]   {len(source_jars)} mill-* source jars")
    for j in sorted(source_jars):
        print(f"[setup-bench]     {j.name}")

    print(f"[setup-bench] Extracting sources -> {SOURCES_DIR} (per-jar subdirs)")
    n = extract_sources(source_jars, SOURCES_DIR)
    print(f"[setup-bench]   {n} source files extracted")

    src_list = sorted(
        str(p.relative_to(SOURCES_DIR))
        for p in SOURCES_DIR.rglob("*")
        if p.is_file() and p.suffix in {".scala", ".java"}
    )
    (INPUTS_DIR / "source-files.txt").write_text("\n".join(src_list) + "\n")
    print(f"[setup-bench]   source-files.txt: {len(src_list)} entries")

    print("[setup-bench] Resolving compile classpath ...")
    cp_jars = cs_fetch([root_coord] + EXTRA_COMPILE_DEPS)
    external = [j for j in cp_jars if not is_mill_jar(j)]
    (INPUTS_DIR / "compile-classpath.txt").write_text(
        "\n".join(str(j) for j in external) + "\n"
    )
    print(f"[setup-bench]   compile-classpath.txt: {len(external)} entries "
          f"(dropped {len(cp_jars) - len(external)} mill-* jars)")

    print("[setup-bench] Resolving scalac plugins ...")
    plugin_jars = [j for j in cs_fetch(PLUGINS) if j.name.endswith(".jar")
                   and any(c.split(":")[1].split("_")[0] in j.name for c in PLUGINS)]
    opts: list[str] = []
    seen: set[str] = set()
    for j in plugin_jars:
        if j.name not in seen:
            opts.append(f"-Xplugin:{j}")
            seen.add(j.name)
    opts += SCALAC_OPTIONS_TAIL
    (INPUTS_DIR / "scalac-options.txt").write_text("\n".join(opts) + "\n")
    print(f"[setup-bench]   scalac-options.txt: {len(opts)} entries "
          f"({sum(1 for o in opts if o.startswith('-Xplugin:'))} plugins)")

    print("[setup-bench] Done.")

if __name__ == "__main__":
    main()
