#!/bin/bash
# Bisect helper: builds the nonbootstrapped compiler and runs the two failing
# tests under -Yno-deep-subtypes. Exit 0 = pass (both compile clean), 1 = fail.
# Exit 125 if the build itself fails (skip this commit in bisect).
set -e
cd "$(dirname "$0")/.."

if ! sbt --error 'scala3-compiler-nonbootstrapped/compile' >/tmp/bisect-build.log 2>&1; then
  echo "BUILD FAILED — skipping commit"
  exit 125
fi

CP=$(sbt --error 'export scala3-compiler-nonbootstrapped/Compile/fullClasspath' | tail -1)
JAVA=/Users/lihaoyi/Library/Java/JavaVirtualMachines/jbr-21.0.9/Contents/Home/bin/java

FLAGS=(-indent -Yno-deep-subtypes -Yno-double-bindings -Xtarget 17 -Ycheck:all -Wconf:id=E222:s)

run_one() {
  local label="$1"; shift
  if "$JAVA" -cp "$CP" dotty.tools.dotc.Main -classpath "$CP" "${FLAGS[@]}" "$@" \
       >/tmp/bisect-$label.log 2>&1
  then
    echo "  $label: PASS"
    return 0
  else
    echo "  $label: FAIL"
    return 1
  fi
}

PASSED=0
run_one i11064 tests/pos/i11064.scala || PASSED=1
run_one i20078 tests/pos/i20078/AbstractShapeBuilder.java tests/pos/i20078/Shape.java tests/pos/i20078/Test.scala tests/pos/i20078/Trait.java || PASSED=1
exit $PASSED
