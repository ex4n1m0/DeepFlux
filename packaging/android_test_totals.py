"""Totals of the Android unit tests from their JUnit XML, and every test that did not run.

Used by .github/workflows/android-build.yml (the unit-tests job) and by hand. Exits non-zero when
anything failed, errored or nothing was found — a job must not look green on an empty result.

Usage (from android/):  python ../packaging/android_test_totals.py [results-dir]
       (from the repo): python packaging/android_test_totals.py android/app/build/test-results/testDebugUnitTest
"""
import glob
import sys
import xml.etree.ElementTree as ET

results = sys.argv[1] if len(sys.argv) > 1 else "app/build/test-results/testDebugUnitTest"
roots = [ET.parse(f).getroot() for f in sorted(glob.glob(results + "/TEST-*.xml"))]
totals = {k: sum(int(r.get(k, 0)) for r in roots) for k in ("tests", "failures", "errors", "skipped")}
print(f"classes {len(roots)} | " + " | ".join(f"{k} {v}" for k, v in totals.items()))
for r in roots:
    for case in r.iter("testcase"):
        skipped = case.find("skipped")
        if skipped is not None:
            name = case.get("classname", "").split(".")[-1] + "." + case.get("name", "")
            print(f"  skipped {name} - {(skipped.get('message') or '')[:140]}")
sys.exit(0 if roots and not totals["failures"] and not totals["errors"] else 1)
