"""Path constants and shell-out helpers shared across the bench scripts."""
from __future__ import annotations
import os, shutil, subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCALA3_DIR = SCRIPT_DIR.parent
BENCH_DIR = SCALA3_DIR / "target" / "bench-mill-javalib"
INPUTS_DIR = BENCH_DIR / "inputs"
SOURCES_DIR = BENCH_DIR / "sources"
BUILD_DIR = BENCH_DIR / "target" / "build"
SRC_DIR = SCRIPT_DIR / "src"

JMH_VERSION = "1.37"

def run(cmd, *, check=True, capture=False, cwd=None, env=None):
    """Run a command. `cmd` is a list of strings."""
    return subprocess.run(
        cmd,
        check=check,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )

def sbt_export(target: str) -> str:
    """Return the last `:`-containing line from `sbt --error 'export <target>'`."""
    out = run(["sbt", "--error", f"export {target}"], capture=True, cwd=SCALA3_DIR).stdout
    cps = [ln for ln in out.splitlines() if ":" in ln]
    if not cps:
        raise SystemExit(f"sbt export {target} produced no classpath line:\n{out}")
    return max(cps, key=len)

def existing_cp(cp: str) -> str:
    return os.pathsep.join(p for p in cp.split(os.pathsep) if p and Path(p).exists())

def coursier_fetch(*coords: str) -> str:
    out = run(["cs", "fetch", "-p", *coords], capture=True).stdout
    return out.strip()
