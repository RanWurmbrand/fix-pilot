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
import time
import json
import threading
from pathlib import Path
from dotenv import load_dotenv

# Setup paths
ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"

load_dotenv(override=True)


# =============================================================================
# Run Report
# =============================================================================

def create_run_report() -> Path:
    """Create a new JSONL run report file."""
    reports_dir = ROOT / "artifacts" / "run_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    report_path = reports_dir / f"run_{timestamp}.jsonl"
    report_path.touch()
    return report_path


def log_to_report(report_path: Path, skill: str, event: str, message: str, **extra):
    """Append a JSONL entry to the run report (used by pipeline itself)."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "skill": skill,
        "event": event,
        "message": message,
        **extra,
    }
    with open(report_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def tail_report(report_path: Path, stop_event: threading.Event):
    """Tail the report file and print new entries in real-time."""
    with open(report_path, "r") as f:
        while not stop_event.is_set():
            line = f.readline()
            if line.strip():
                try:
                    entry = json.loads(line.strip())
                    event = entry.get("event", "?")
                    message = entry.get("message", "")
                    skill = entry.get("skill", "?")
                    agent = entry.get("agent", "")
                    agent_str = f" ({agent})" if agent else ""
                    print(f"  [{skill}] {event}{agent_str}: {message}", flush=True)
                except json.JSONDecodeError:
                    print(f"  {line.strip()}", flush=True)
            else:
                stop_event.wait(0.5)


# =============================================================================
# Helpers
# =============================================================================

def run_claude_skill(skill_name: str, timeout: int = 7200, cwd: str = None, report_path: Path = None) -> bool:
    """
    Run a Claude skill and return success/failure.
    """
    skill_path = SKILLS_DIR / f"{skill_name}.skill.md"

    if not skill_path.exists():
        print(f"  ERROR: Skill not found: {skill_path}")
        return False

    skill_content = skill_path.read_text()
    work_dir = cwd or str(ROOT)

    prompt = "Execute the task defined in the system prompt."

    # Add target project dir for skills that need project access
    project_path = os.getenv("PROJECT_PATH")
    if skill_name in ("test-replicator", "trace-analyzer", "bug-fixer", "fix-applier") and project_path:
        prompt += f" The target project is at: {project_path}"

    # Pass test command to test-replicator
    if skill_name == "test-replicator":
        execute_command = os.getenv("EXECUTE_COMMAND", "npm test")
        prompt += f" Execute command: {execute_command}"

    # Add report path
    if report_path:
        prompt += f" Run report file: {report_path}"

    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--add-dir", str(ROOT),
    ]

    if skill_name in ("test-replicator", "trace-analyzer", "bug-fixer", "fix-applier") and project_path:
        cmd.extend(["--add-dir", project_path])

    cmd.extend(["--system-prompt", skill_content, prompt])

    # Log skill start
    if report_path:
        log_to_report(report_path, skill_name, "started", f"Pipeline launched {skill_name}")

    # Start tailing the report file
    stop_event = None
    tail_thread = None
    if report_path:
        stop_event = threading.Event()
        tail_thread = threading.Thread(target=tail_report, args=(report_path, stop_event), daemon=True)
        tail_thread.start()

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            timeout=timeout,
            stdin=subprocess.DEVNULL
        )
        success = result.returncode == 0
        if report_path:
            status = "success" if success else "failed"
            log_to_report(report_path, skill_name, "finished", f"{skill_name} {status} (exit code {result.returncode})")
        return success

    except subprocess.TimeoutExpired:
        print(f"  ERROR: Skill timed out after {timeout}s")
        if report_path:
            log_to_report(report_path, skill_name, "error", f"Timed out after {timeout}s")
        return False
    except FileNotFoundError:
        print("  ERROR: Claude CLI not found")
        if report_path:
            log_to_report(report_path, skill_name, "error", "Claude CLI not found")
        return False
    finally:
        if stop_event:
            stop_event.set()
        if tail_thread:
            tail_thread.join(timeout=2)


# =============================================================================
# Pipeline Steps
# =============================================================================

def step_run_tests(report_path: Path = None) -> tuple[bool, bool]:
    """Run the test suite. Returns (success, all_passed)."""
    print("\n[1/6] Running tests...")

    # Import here to avoid circular issues
    sys.path.insert(0, str(ROOT))
    from core.project_runner import ProjectRunner

    project_path = os.getenv("PROJECT_PATH")
    command = os.getenv("EXECUTE_COMMAND", "npm test")

    if not project_path:
        print("  ERROR: PROJECT_PATH not set in .env")
        return False, False

    try:
        if report_path:
            log_to_report(report_path, "pipeline", "step", f"Running tests: {command}")
        runner = ProjectRunner(project_path, command)
        log_file, exit_code, _ = runner.run()
        print(f"  Log: {log_file}")
        print(f"  Exit code: {exit_code}")
        if report_path:
            log_to_report(report_path, "pipeline", "step", f"Tests finished. Exit code: {exit_code}. Log: {log_file}")
        all_passed = exit_code == 0
        return True, all_passed
    except Exception as e:
        print(f"  ERROR: {e}")
        if report_path:
            log_to_report(report_path, "pipeline", "error", f"Test execution error: {e}")
        return False, False


def step_replicate(report_path: Path = None) -> bool:
    """Capture DOM at failure point using test-replicator skill."""
    print("\n[2/6] Capturing DOM...")
    return run_claude_skill("test-replicator", timeout=3600, report_path=report_path)


def step_analyze(report_path: Path = None) -> bool:
    """Analyze test failure using trace-analyzer skill."""
    print("\n[3/6] Analyzing failure...")
    return run_claude_skill("trace-analyzer", timeout=3600, report_path=report_path)


def step_generate_fix(report_path: Path = None) -> bool:
    """Generate fix using bug-fixer skill."""
    print("\n[4/6] Generating fix...")
    return run_claude_skill("bug-fixer", timeout=3600, report_path=report_path)


def step_notify_success():
    """Send Telegram notification that all tests passed."""
    print("\nNotifying user: All tests passed!")

    from messaging.telegram_manager import TelegramManager

    tm = TelegramManager()
    tm.send_message("✅ All tests passed! No issues found.")


def step_notify() -> str:
    """Send Telegram notification and wait for user response."""
    print("\n[5/6] Sending notification...")

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


def step_apply_fix(report_path: Path = None) -> bool:
    """Apply fix using fix-applier skill."""
    print("\n[6/6] Applying fix...")
    return run_claude_skill("fix-applier", timeout=3600, report_path=report_path)


# =============================================================================
# Main Pipeline
# =============================================================================

def run_pipeline():
    """Run the full pipeline with loop support."""

    print("=" * 50)
    print("RootCause AI Pipeline")
    print("=" * 50)

    report_path = create_run_report()
    print(f"Run report: {report_path}")

    while True:
        # Step 1: Run tests
        success, all_passed = step_run_tests(report_path)
        if not success:
            print("\nPipeline stopped: Test execution failed")
            return False

        # If all tests passed, notify and finish
        if all_passed:
            print("\n✓ All tests passed!")
            step_notify_success()
            return True

        # Step 2: Capture DOM
        if not step_replicate(report_path):
            print("\nPipeline stopped: DOM capture failed")
            return False

        # Step 3: Analyze
        if not step_analyze(report_path):
            print("\nPipeline stopped: Analysis failed")
            return False

        # Step 4: Generate fix
        if not step_generate_fix(report_path):
            print("\nPipeline stopped: Fix generation failed")
            return False

        # Step 5: Notify and get user action
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
            # Step 6: Apply fix
            if not step_apply_fix(report_path):
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
