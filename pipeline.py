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

import subprocess
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Setup paths
ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"

load_dotenv(override=True)


# =============================================================================
# Helpers
# =============================================================================

def run_claude_skill(skill_name: str, timeout: int = 360, cwd: str = None) -> bool:
    """
    Run a Claude skill and return success/failure.
    """
    skill_path = SKILLS_DIR / f"{skill_name}.skill.md"

    if not skill_path.exists():
        print(f"  ERROR: Skill not found: {skill_path}")
        return False

    skill_content = skill_path.read_text()
    work_dir = cwd or str(ROOT)

    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--add-dir", str(ROOT),
        "--system-prompt", skill_content,
        "Execute the task defined in the system prompt"
    ]

    # Add target project dir for fix-applier
    if skill_name == "fix-applier":
        project_path = os.getenv("PROJECT_PATH")
        if project_path:
            cmd.insert(4, "--add-dir")
            cmd.insert(5, project_path)

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            timeout=timeout,
            stdin=subprocess.DEVNULL
        )
        return result.returncode == 0

    except subprocess.TimeoutExpired:
        print(f"  ERROR: Skill timed out after {timeout}s")
        return False
    except FileNotFoundError:
        print("  ERROR: Claude CLI not found")
        return False


# =============================================================================
# Pipeline Steps
# =============================================================================

def step_run_tests() -> bool:
    """Run the test suite."""
    print("\n[1/5] Running tests...")

    # Import here to avoid circular issues
    sys.path.insert(0, str(ROOT))
    from core.project_runner import ProjectRunner

    project_path = os.getenv("PROJECT_PATH")
    command = os.getenv("EXECUTE_COMMAND", "npm test")

    if not project_path:
        print("  ERROR: PROJECT_PATH not set in .env")
        return False

    try:
        runner = ProjectRunner(project_path, command)
        log_file, exit_code, _ = runner.run()
        print(f"  Log: {log_file}")
        print(f"  Exit code: {exit_code}")
        return True  # Always continue to analysis
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def step_analyze() -> bool:
    """Analyze test failure using trace-analyzer skill."""
    print("\n[2/5] Analyzing failure...")
    return run_claude_skill("trace-analyzer", timeout=360)


def step_generate_fix() -> bool:
    """Generate fix using bug-fixer skill."""
    print("\n[3/5] Generating fix...")
    return run_claude_skill("bug-fixer", timeout=360)


def step_notify() -> str:
    """Send Telegram notification and wait for user response."""
    print("\n[4/5] Sending notification...")

    from messaging.bugfix_notifier import BugFixMessageBuilder
    from messaging.telegram_manager import TelegramManager
    import json
    import time

    builder = BugFixMessageBuilder()
    message, is_long = builder.build_message()

    tm = TelegramManager()

    if is_long:
        tm.send_document(message, "bugfix_summary.html", "Bug Fix Summary (see attached)")
        tm.send_bugfix_message("Full report sent as file. Choose action:")
    else:
        tm.send_bugfix_message(message)

    print("  Waiting for user response...")
    action = tm.wait_for_user_response()
    print(f"  User chose: {action}")

    # Handle suggestion flow
    if action == "suggest":
        tm.send_message("Please provide your suggestion:")
        suggestion = tm.wait_for_text_message()
        print(f"  Received suggestion: {suggestion[:50]}...")

        # Save suggestion
        suggestions_dir = ROOT / "artifacts" / "suggestions"
        suggestions_dir.mkdir(exist_ok=True)

        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        suggestion_file = suggestions_dir / f"suggestion_{timestamp}.json"
        suggestion_file.write_text(json.dumps({
            "timestamp": timestamp,
            "user_suggestion": suggestion,
            "action": "re_analyze"
        }, indent=2))

        tm.send_message("Got it! Re-analyzing...")
        return "suggest"

    return action


def step_apply_fix() -> bool:
    """Apply fix using fix-applier skill."""
    print("\n[5/5] Applying fix...")
    project_path = os.getenv("PROJECT_PATH", str(ROOT))
    return run_claude_skill("fix-applier", timeout=360, cwd=project_path)


# =============================================================================
# Main Pipeline
# =============================================================================

def run_pipeline():
    """Run the full pipeline with loop support."""

    print("=" * 50)
    print("RootCause AI Pipeline")
    print("=" * 50)

    while True:
        # Step 1: Run tests
        if not step_run_tests():
            print("\nPipeline stopped: Test execution failed")
            return False

        # Step 2: Analyze
        if not step_analyze():
            print("\nPipeline stopped: Analysis failed")
            return False

        # Step 3: Generate fix
        if not step_generate_fix():
            print("\nPipeline stopped: Fix generation failed")
            return False

        # Step 4: Notify and get user action
        action = step_notify()

        if action == "terminate":
            print("\nPipeline stopped by user")
            return True

        if action == "rerun":
            print("\nRerunning tests...")
            continue

        if action == "suggest":
            print("\nRe-analyzing with user suggestion...")
            continue

        if action == "fix_and_rerun":
            # Step 5: Apply fix
            if not step_apply_fix():
                print("\nPipeline stopped: Fix application failed")
                return False
            print("\nFix applied. Rerunning tests...")
            continue

        # Unknown action
        print(f"\nUnknown action: {action}")
        return False


if __name__ == "__main__":
    try:
        success = run_pipeline()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
