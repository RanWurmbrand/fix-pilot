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

import sys
from pathlib import Path
from dotenv import load_dotenv

from core.pipeline import Pipeline

# Setup paths
ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"

load_dotenv(override=True)


if __name__ == "__main__":
    try:
        pipeline = Pipeline(ROOT, SKILLS_DIR)
        success = pipeline.run()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
