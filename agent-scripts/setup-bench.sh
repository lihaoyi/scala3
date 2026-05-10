#!/usr/bin/env bash
#
# setup-bench.sh — One-time setup for the bench-mill-javalib JMH benchmark.
#
# What it does:
#   1. Walks Mill's libs.javalib transitive module deps and copies all
#      .scala/.java sources into bench-mill-javalib/sources/.
#   2. Captures the resolved external (ivy) classpath, the scalac plugin
#      classpath, and the scalac options into bench-mill-javalib/inputs/.
#
# Inputs are written as plain text files so the JMH benchmark can read them
# at runtime without invoking Mill again.
#
# This script does NOT need to be re-run unless Mill's deps or sources change.

set -euo pipefail

SCALA3_DIR="/Users/lihaoyi/Github/scala3"
MILL_DIR="/Users/lihaoyi/Github/mill"
BENCH_DIR="${SCALA3_DIR}/bench-mill-javalib"
SOURCES_DIR="${BENCH_DIR}/sources"
INPUTS_DIR="${BENCH_DIR}/inputs"

# All transitive Mill modules that libs.javalib depends on (computed once
# from `./mill show <module>.showModuleDeps`).
MODULES=(
  libs.javalib
  libs.util
  libs.rpc
  libs.javalib.api
  libs.javalib.testrunner
  core.api
  core.api.daemon
  core.api.java11
  core.constants
  libs.daemon.server
  libs.daemon.client
  libs.util.java11
  libs.javalib.testrunner.entrypoint
)

echo "[setup-bench] Mill repo: ${MILL_DIR}"
echo "[setup-bench] Bench dir: ${BENCH_DIR}"
echo "[setup-bench] Modules (${#MODULES[@]}):"
for m in "${MODULES[@]}"; do echo "    $m"; done

mkdir -p "${SOURCES_DIR}" "${INPUTS_DIR}"

# Wipe previous source copies (keep dir).
rm -rf "${SOURCES_DIR:?}"/*

# Build a comma-joined Mill selector like {a,b,c}
JOINED=$(IFS=, ; echo "${MODULES[*]}")
SELECTOR="{${JOINED}}"

echo "[setup-bench] Querying Mill for source files of all modules..."
ALL_SOURCE_FILES_JSON="${INPUTS_DIR}/_allSourceFiles.json"
( cd "${MILL_DIR}" && ./mill show "${SELECTOR}.allSourceFiles" ) > "${ALL_SOURCE_FILES_JSON}"

# Extract bare absolute paths from Mill's "ref:vN:hash:/path" entries.
# JSON shape is { "module.name": [ "ref:v0:abc:/abs/path", ... ], ... }
SOURCE_PATHS_TXT="${INPUTS_DIR}/source-paths.txt"
python3 - "${ALL_SOURCE_FILES_JSON}" "${SOURCE_PATHS_TXT}" <<'PY'
import json, re, sys
src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src))
out = []
def extract(v):
    # "ref:vN:HASH:/abs/path"
    m = re.match(r'^[a-z]*ref:v\d+:[0-9a-f]+:(.*)$', v)
    return m.group(1) if m else v
def walk(x):
    if isinstance(x, list):
        for e in x: walk(e)
    elif isinstance(x, dict):
        for vv in x.values(): walk(vv)
    elif isinstance(x, str):
        out.append(extract(x))
walk(data)
with open(dst, 'w') as f:
    for p in out:
        f.write(p + "\n")
print(f"[setup-bench] {len(out)} source paths -> {dst}")
PY

echo "[setup-bench] Copying source files into ${SOURCES_DIR} ..."
COUNT=0
while IFS= read -r src; do
    [ -z "$src" ] && continue
    if [ ! -f "$src" ]; then
        echo "[setup-bench] WARNING: missing source file: $src" >&2
        continue
    fi
    # Mirror layout under SOURCES_DIR using the path relative to MILL_DIR
    # (so package directives still match).
    rel="${src#${MILL_DIR}/}"
    dst="${SOURCES_DIR}/${rel}"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    COUNT=$((COUNT+1))
done < "${SOURCE_PATHS_TXT}"
echo "[setup-bench] Copied ${COUNT} source files."

# Write the list of copied source files (relative to SOURCES_DIR / absolute).
COPIED_LIST="${INPUTS_DIR}/source-files.txt"
( cd "${SOURCES_DIR}" && find . -type f \( -name '*.scala' -o -name '*.java' \) | sed 's|^\./||' | sort ) > "${COPIED_LIST}"
echo "[setup-bench] Source file list -> ${COPIED_LIST} ($(wc -l < "${COPIED_LIST}") files)"

echo "[setup-bench] Capturing UNION of compileClasspath for all modules ..."
ALL_CC_JSON="${INPUTS_DIR}/_allCompileClasspaths.json"
( cd "${MILL_DIR}" && ./mill show "${SELECTOR}.compileClasspath" ) > "${ALL_CC_JSON}"

echo "[setup-bench] Capturing libs.javalib.allScalacOptions (includes -Xplugin entries) ..."
ALL_OPTS_JSON="${INPUTS_DIR}/_allScalacOptions.json"
( cd "${MILL_DIR}" && ./mill show libs.javalib.allScalacOptions ) > "${ALL_OPTS_JSON}"

# Decode the JSON and emit plain-text artifacts:
#   compile-classpath.txt   : one absolute jar path per line (third-party deps,
#                              union of all transitive modules; INTERNAL Mill
#                              compile-output paths are dropped because we are
#                              compiling those sources fresh ourselves).
#   scalac-options.txt      : one option per line, INCLUDING the
#                              `-Xplugin:/abs/path/to/plugin.jar` entries
python3 - "${ALL_CC_JSON}" "${INPUTS_DIR}/compile-classpath.txt" \
                "${ALL_OPTS_JSON}" "${INPUTS_DIR}/scalac-options.txt" \
                "${MILL_DIR}" <<'PY'
import json, re, sys
mill_dir = sys.argv[5].rstrip('/')
def extract(v):
    m = re.match(r'^[a-z]*ref:v\d+:[0-9a-f]+:(.*)$', v)
    return m.group(1) if m else v

cp_data = json.load(open(sys.argv[1]))
seen = set()
union = []
for module_name, entries in cp_data.items():
    for raw in entries:
        p = extract(raw)
        # Drop internal Mill build outputs (compile.dest) and compile-resources
        # dirs that point to non-existent/empty roots in the source tree.
        # We are about to RECOMPILE those modules ourselves so their classes
        # would be stale anyway; including them would also let dotc see types
        # from the published artifacts and never observe types we are
        # supposed to rebuild.
        if p.startswith(mill_dir + '/out/'): continue
        if p.startswith(mill_dir + '/') and p.endswith('/compile-resources'): continue
        if p in seen: continue
        seen.add(p)
        union.append(p)
opts = json.load(open(sys.argv[3]))
with open(sys.argv[2], 'w') as f:
    for p in union: f.write(p + "\n")
with open(sys.argv[4], 'w') as f:
    for o in opts: f.write(o + "\n")
plugin_count = sum(1 for o in opts if o.startswith("-Xplugin:"))
print(f"compile-classpath.txt: {len(union)} entries (union of {len(cp_data)} modules)")
print(f"scalac-options.txt:    {len(opts)} entries ({plugin_count} -Xplugin)")
PY

echo "[setup-bench] Done."
echo "[setup-bench] Now run ./agent-scripts/run-bench.sh to build the local compiler and run JMH."
