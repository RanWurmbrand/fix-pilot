#!/usr/bin/env python3
"""
RootCause AI Pipeline

A single script that runs the full bug detection and fixing flow:
  1. Run tests
  2. Analyze failure (trace-analyzer skill)
  3. Generate fix (bug-fixer skill)
  4. Notify via Telegram
  5. Apply fix if approved (fix-applier skill)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

from core.pipeline import Pipeline

# Setup paths
ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"

load_dotenv(override=True)


def fetch_nightly_failures():
    """Run get_failing_tests.sh and return list of failing test paths."""
    script = ROOT / "scripts" / "get_failing_tests.sh"
    subprocess.run([str(script)], check=True)

    failing_tests_file = ROOT / "artifacts" / "failing_tests.json"
    if failing_tests_file.exists():
        return json.loads(failing_tests_file.read_text())
    return []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RootCause AI Pipeline")
    parser.add_argument("--nightly", action="store_true",
                        help="Fetch failing tests from nightly CI and run pipeline on each")
    parser.add_argument("--ft", action="store_true",
                        help="Run pipeline on existing failing_tests.json without fetching")
    args = parser.parse_args()

    try:
        if args.ft:
            failing_tests_file = ROOT / "artifacts" / "failing_tests.json"
            if not failing_tests_file.exists():
                print("No failing_tests.json found. Run --nightly first.")
                sys.exit(1)
            failing_tests = json.loads(failing_tests_file.read_text())
        elif args.nightly:
            failing_tests = fetch_nightly_failures()
        else:
            failing_tests = None

        if failing_tests is not None:
            print(f"Found {len(failing_tests)} failing tests")
            for test in failing_tests:
                print(f"  - {test}")

            results = []
            for i, test in enumerate(failing_tests, 1):
                print(f"\n{'='*50}")
                print(f"[{i}/{len(failing_tests)}] Running pipeline for: {test}")
                print("="*50)

                import os
                os.environ["EXECUTE_COMMAND"] = f'npm run e2e:run:local -- --headed --spec "{test}"'

                pipeline = Pipeline(ROOT, SKILLS_DIR)
                success = pipeline.run()
                results.append({"test": test, "success": success})

                if not success:
                    print(f"\nPipeline failed for {test}, continuing to next...")

            print(f"\n{'='*50}")
            print("Run complete")
            print("="*50)
            passed = sum(1 for r in results if r["success"])
            print(f"Results: {passed}/{len(results)} succeeded")
            for r in results:
                status = "✓" if r["success"] else "✗"
                print(f"  {status} {r['test']}")

            sys.exit(0 if passed == len(results) else 1)

        pipeline = Pipeline(ROOT, SKILLS_DIR)
        success = pipeline.run()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
