#!/usr/bin/env python3
"""Run the functional scenarios against local images.

Example: python3 image/functional/run.py --image all=quay.io/ceph/ceph:v20.2.4
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suite  # noqa: E402


def images_from(values):
    images = {}
    for value in values:
        role, separator, reference = value.partition("=")
        if not separator or role not in suite.ROLES + ("all",) or not reference:
            raise SystemExit("Expected ROLE=REF with role in " + ", ".join(suite.ROLES + ("all",)))
        images[role] = reference
    if "all" in images:
        images = dict({role: images["all"] for role in suite.ROLES}, **{
            role: reference for role, reference in images.items() if role != "all"})
    return images


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", action="append", required=True, metavar="ROLE=REF",
                        help="Image for control, osd, rgw, mds or all; roles override all")
    parser.add_argument("--scenario", action="append", choices=suite.SCENARIOS,
                        help="Scenario to run; repeatable; default is every scenario")
    parser.add_argument("--output-dir", type=Path, help="Log directory (default: artifacts/functional-UUID)")
    args = parser.parse_args(argv)
    output = (args.output_dir or Path("artifacts") / ("functional-" + uuid.uuid4().hex[:8])).resolve()
    results = suite.run(images_from(args.image), args.scenario or list(suite.SCENARIOS), output)
    (output / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print("Results: " + str(output / "results.json"))
    return 0 if all(result["status"] == "passed" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
