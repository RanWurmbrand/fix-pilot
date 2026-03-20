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
import shutil
import random
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
# Fix History
# =============================================================================

def get_project_head_commit() -> str:
    """Get the current HEAD commit hash of the target project."""
    project_path = os.getenv("PROJECT_PATH")
    if not project_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def init_fix_history(session_name: str) -> Path:
    """Create fix_history.json at pipeline start."""
    artifacts_dir = ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    fix_history_path = artifacts_dir / "fix_history.json"
    fix_history = {
        "session": session_name,
        "base_commit": get_project_head_commit(),
        "supervisor_interventions": 0,
        "attempts": [],
    }
    fix_history_path.write_text(json.dumps(fix_history, indent=2))
    return fix_history_path


def record_attempt(fix_history_path: Path, result: str, error_after: str = None):
    """Record a fix attempt in fix_history.json."""
    history = json.loads(fix_history_path.read_text())
    attempt_num = len(history["attempts"]) + 1

    # Read latest hint file for cause
    hints_dir = ROOT / "artifacts" / "hints"
    hint_files = sorted(hints_dir.glob("hint_*.json"), key=lambda f: f.stat().st_mtime)
    cause = ""
    if hint_files:
        try:
            hint = json.loads(hint_files[-1].read_text())
            cause = hint.get("cause", "")
        except (json.JSONDecodeError, OSError):
            pass

    # Read latest fix file for patch info
    fixes_dir = ROOT / "artifacts" / "bug_fixes"
    fix_files = sorted(fixes_dir.glob("fix_*.json"), key=lambda f: f.stat().st_mtime)
    fix_applied = ""
    files_changed = []
    if fix_files:
        try:
            fix = json.loads(fix_files[-1].read_text())
            fix_applied = fix.get("patch_suggestion", "")
            files_changed = fix.get("functions_to_edit", [])
        except (json.JSONDecodeError, OSError):
            pass

    attempt = {
        "attempt": attempt_num,
        "cause": cause,
        "fix_applied": fix_applied,
        "files_changed": files_changed,
        "commit_hash": get_project_head_commit(),
        "result": result,
    }
    if error_after:
        attempt["error_after"] = error_after

    history["attempts"].append(attempt)
    fix_history_path.write_text(json.dumps(history, indent=2))


def extract_error_summary(log_path: Path, max_chars: int = 200) -> str:
    """Extract the actual test assertion error from a log file."""
    try:
        content = log_path.read_text(encoding="utf-8", errors="ignore")
        lines = content.strip().split("\n")
        # Look for the actual test error (AssertionError, TypeError, etc.)
        # Search top-down — first assertion/test error is the real one
        for line in lines:
            stripped = line.strip()
            if any(kw in stripped for kw in ["AssertionError:", "AssertionError:", "TypeError:", "Error:", "TimeoutError:"]):
                # Skip npm wrapper errors and generic lines
                if "npm error" in stripped or "command failed" in stripped or "exit code" in stripped:
                    continue
                return stripped[:max_chars]
        # Fallback: last non-empty line that isn't npm noise
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and "npm error" not in stripped:
                return stripped[:max_chars]
    except OSError:
        pass
    return ""


# =============================================================================
# Helpers
# =============================================================================

def extract_test_name_from_command(command: str) -> str:
    """Extract test name from EXECUTE_COMMAND for branch naming."""
    import re

    # Try to extract spec file from Cypress command (--spec "path/to/test.cy.ts")
    spec_match = re.search(r'--spec\s+["\']?([^"\']+)["\']?', command)
    if spec_match:
        spec_path = spec_match.group(1)
        # Get filename without extension (e.g., "my-test" from "cypress/e2e/my-test.cy.ts")
        filename = Path(spec_path).stem
        # Remove .cy suffix if present
        if filename.endswith('.cy'):
            filename = filename[:-3]
        return filename

    # Fallback: use last non-flag argument
    parts = command.split()
    for part in reversed(parts):
        if not part.startswith('-') and '/' not in part and part not in ('npx', 'npm', 'yarn', 'run', 'test', 'cypress'):
            return part

    return "test"


def sanitize_branch_name(name: str) -> str:
    """Sanitize a string to be a valid git branch name."""
    import re
    # Replace spaces and invalid chars with hyphens
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', name)
    # Remove consecutive hyphens
    sanitized = re.sub(r'-+', '-', sanitized)
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip('-')
    return sanitized.lower() or "test"


def create_fix_branch() -> str:
    """Create a new branch for this fix attempt. Returns branch name or empty string on failure."""
    project_path = os.getenv("PROJECT_PATH")
    if not project_path:
        print("  ERROR: PROJECT_PATH not set")
        return ""

    # Extract test name from command
    execute_command = os.getenv("EXECUTE_COMMAND", "")
    test_name = extract_test_name_from_command(execute_command)
    test_name = sanitize_branch_name(test_name)

    # Generate branch name with random 8 digits
    random_suffix = str(random.randint(10000000, 99999999))
    branch_name = f"{test_name}-fix-{random_suffix}"

    try:
        # Create and checkout new branch
        result = subprocess.run(
            ["git", "-C", project_path, "checkout", "-b", branch_name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"  ERROR: Failed to create branch: {result.stderr}")
            return ""

        print(f"  Created branch: {branch_name}")
        return branch_name

    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ERROR: {e}")
        return ""


def run_claude_skill(skill_name: str, timeout: int = 7200, cwd: str = None, report_path: Path = None, fix_history_path: Path = None) -> bool:
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
    if skill_name in ("test-replicator", "trace-analyzer", "bug-fixer", "fix-applier", "fix-supervisor", "commit-fixes") and project_path:
        prompt += f" The target project is at: {project_path}"

    # Pass test command to test-replicator
    if skill_name == "test-replicator":
        execute_command = os.getenv("EXECUTE_COMMAND", "npm test")
        prompt += f" Execute command: {execute_command}"

    # Add fix history path for trace-analyzer, bug-fixer, and fix-supervisor
    if skill_name in ("trace-analyzer", "bug-fixer", "fix-supervisor") and fix_history_path:
        prompt += f" Fix history file: {fix_history_path}"

    # Add guidance path for trace-analyzer and bug-fixer if supervisor has written one
    if skill_name in ("trace-analyzer", "bug-fixer"):
        guidance_path = ROOT / "artifacts" / "strategy" / "guidance.json"
        if guidance_path.exists():
            prompt += f" Supervisor guidance file: {guidance_path}"

    # Add report path
    if report_path:
        prompt += f" Run report file: {report_path}"

    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--model", "claude-opus-4-5",
        "--add-dir", str(ROOT),
    ]

    if skill_name in ("test-replicator", "trace-analyzer", "bug-fixer", "fix-applier", "fix-supervisor", "commit-fixes") and project_path:
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
# Artifact Archiving
# =============================================================================

def archive_run_artifacts(session_name: str):
    """Archive artifacts from the current run into a timestamped folder."""
    artifacts_dir = ROOT / "artifacts"
    archive_base = ROOT / "previous_artifacts"
    archive_base.mkdir(parents=True, exist_ok=True)

    # Create archive folder for this run
    run_archive = archive_base / session_name
    run_archive.mkdir(exist_ok=True)

    # Items to archive from this run
    items_to_archive = [
        "run_reports",
        "hints",
        "bug_fixes",
        "rootcause_logs",
        "strategy",
        "suggestions",
        "fix_history.json",
        "dom_snapshots",
    ]

    for item in items_to_archive:
        src = artifacts_dir / item
        if src.exists():
            dst = run_archive / item
            if src.is_file():
                shutil.copy2(src, dst)
            else:
                shutil.copytree(src, dst, dirs_exist_ok=True)

    print(f"Archived artifacts to: {run_archive}")


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


def step_analyze(report_path: Path = None, fix_history_path: Path = None) -> bool:
    """Analyze test failure using trace-analyzer skill."""
    print("\n[3/6] Analyzing failure...")
    return run_claude_skill("trace-analyzer", timeout=3600, report_path=report_path, fix_history_path=fix_history_path)


def step_generate_fix(report_path: Path = None, fix_history_path: Path = None) -> bool:
    """Generate fix using bug-fixer skill."""
    print("\n[4/6] Generating fix...")
    return run_claude_skill("bug-fixer", timeout=3600, report_path=report_path, fix_history_path=fix_history_path)


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


def step_commit_fixes(report_path: Path = None) -> bool:
    """Commit all fixes using commit-fixes skill."""
    print("\nCommitting fixes...")
    return run_claude_skill("commit-fixes", timeout=600, report_path=report_path)


def step_supervise(report_path: Path = None, fix_history_path: Path = None) -> bool:
    """Run the fix supervisor to analyze failure patterns and redirect."""
    print("\n[SUPERVISOR] Analyzing fix history...")
    return run_claude_skill("fix-supervisor", timeout=3600, report_path=report_path, fix_history_path=fix_history_path)


def apply_supervisor_guidance(fix_history_path: Path, report_path: Path = None) -> bool:
    """Read supervisor guidance and apply git reset if needed. Returns False if escalating."""
    guidance_path = ROOT / "artifacts" / "strategy" / "guidance.json"
    if not guidance_path.exists():
        print("  WARNING: Supervisor did not produce guidance file")
        return True

    try:
        guidance = json.loads(guidance_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: Could not read guidance: {e}")
        return True

    # Check for escalation
    if guidance.get("escalate"):
        print("  Supervisor recommends escalation — problem may require manual intervention")
        if report_path:
            log_to_report(report_path, "pipeline", "supervisor_escalate", guidance.get("analysis", ""))
        return False

    # Apply git reset if rewind_commit is specified
    rewind_commit = guidance.get("rewind_commit")
    if not rewind_commit:
        print("  Supervisor provided guidance but no rewind needed")
        return True

    # Safety check: verify rewind_commit is at or after base_commit
    history = json.loads(fix_history_path.read_text())
    base_commit = history.get("base_commit", "")
    project_path = os.getenv("PROJECT_PATH")

    if base_commit and project_path:
        # Check if base_commit is an ancestor of rewind_commit (rewind is at or after base)
        result = subprocess.run(
            ["git", "-C", project_path, "merge-base", "--is-ancestor", base_commit, rewind_commit],
            capture_output=True, timeout=10
        )
        if result.returncode != 0:
            print(f"  SAFETY: Refusing to reset — {rewind_commit[:8]} is before base commit {base_commit[:8]}")
            if report_path:
                log_to_report(report_path, "pipeline", "supervisor_safety_block",
                              f"Refused reset to {rewind_commit[:8]}, before base {base_commit[:8]}")
            return True

    # Do the reset
    print(f"  Rewinding to commit {rewind_commit[:8]} (attempt {guidance.get('rewind_to_attempt', '?')})...")
    result = subprocess.run(
        ["git", "-C", project_path, "reset", "--hard", rewind_commit],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"  ERROR: Git reset failed: {result.stderr}")
        if report_path:
            log_to_report(report_path, "pipeline", "error", f"Git reset failed: {result.stderr}")
        return True

    print(f"  Rewound successfully. Direction: {guidance.get('direction', 'N/A')}")
    if report_path:
        log_to_report(report_path, "pipeline", "supervisor_rewind",
                      f"Reset to {rewind_commit[:8]}. Direction: {guidance.get('direction', '')}")

    # Track intervention count
    history["supervisor_interventions"] = history.get("supervisor_interventions", 0) + 1
    fix_history_path.write_text(json.dumps(history, indent=2))

    return True


# =============================================================================
# Main Pipeline
# =============================================================================

def run_pipeline():
    """Run the full pipeline with loop support."""

    print("=" * 50)
    print("RootCause AI Pipeline")
    print("=" * 50)

    # Create a new branch for this fix attempt
    print("\nCreating fix branch...")
    branch_name = create_fix_branch()
    if not branch_name:
        print("Pipeline stopped: Could not create fix branch")
        return False

    report_path = create_run_report()
    print(f"Run report: {report_path}")

    session_name = report_path.stem  # e.g. "run_2026-03-14_19-44-57"
    fix_history_path = init_fix_history(session_name)
    print(f"Fix history: {fix_history_path}")

    # Clear stale supervisor guidance from previous runs
    guidance_path = ROOT / "artifacts" / "strategy" / "guidance.json"
    if guidance_path.exists():
        guidance_path.unlink()
        print("Cleared stale supervisor guidance")

    skip_tests = False
    supervisor_just_ran = False

    while True:
        # Step 1: Run tests (skipped if we just ran them in fix_and_rerun)
        if not skip_tests:
            success, all_passed = step_run_tests(report_path)
            if not success:
                print("\nPipeline stopped: Test execution failed")
                return False

            # If all tests passed on first run, no fixes needed
            if all_passed:
                print("\n✓ All tests passed!")
                archive_run_artifacts(session_name)
                step_notify_success()
                return True
        skip_tests = False

        # Step 2: Capture DOM (skip if failing line is same as previous attempt)
        should_replicate = True
        logs_dir = ROOT / "artifacts" / "rootcause_logs"
        log_files = sorted(logs_dir.glob("run_*.log"), key=lambda f: f.stat().st_mtime)
        if log_files:
            current_error = extract_error_summary(log_files[-1])
            history = json.loads(fix_history_path.read_text())
            if history["attempts"]:
                prev_error = history["attempts"][-1].get("error_after", "")
                if current_error and prev_error and current_error == prev_error:
                    print("\n[2/6] Skipping DOM capture — same failing line as previous attempt")
                    should_replicate = False

        if should_replicate:
            if not step_replicate(report_path):
                print("\nPipeline stopped: DOM capture failed")
                return False

        # Step 3: Analyze
        if not step_analyze(report_path, fix_history_path):
            print("\nPipeline stopped: Analysis failed")
            return False

        # Step 4: Generate fix
        if not step_generate_fix(report_path, fix_history_path):
            print("\nPipeline stopped: Fix generation failed")
            return False

        # Step 5: Notify and get user action
        action = step_notify()

        # If supervisor just intervened, send a note about it
        if supervisor_just_ran:
            from messaging.telegram_manager import TelegramManager
            guidance_path = ROOT / "artifacts" / "strategy" / "guidance.json"
            if guidance_path.exists():
                try:
                    guidance = json.loads(guidance_path.read_text())
                    rewind_to = guidance.get("rewind_to_attempt", "?")
                    analysis = guidance.get("analysis", "")
                    TelegramManager().send_message(
                        f"🔄 Supervisor intervened: rewound to attempt {rewind_to}.\n{analysis}"
                    )
                except (json.JSONDecodeError, OSError):
                    pass
            supervisor_just_ran = False

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

            # Run tests after fix
            print("\nFix applied. Rerunning tests...")
            success, all_passed = step_run_tests(report_path)
            if not success:
                record_attempt(fix_history_path, "failed", "Test execution failed")
                print("\nPipeline stopped: Test execution failed")
                return False

            if all_passed:
                record_attempt(fix_history_path, "passed")
                print("\n✓ All tests passed after fix!")
                step_commit_fixes(report_path)
                archive_run_artifacts(session_name)
                step_notify_success()
                return True

            # Tests still failing — record attempt with error summary
            logs_dir = ROOT / "artifacts" / "rootcause_logs"
            log_files = sorted(logs_dir.glob("run_*.log"), key=lambda f: f.stat().st_mtime)
            error_summary = extract_error_summary(log_files[-1]) if log_files else ""
            record_attempt(fix_history_path, "failed", error_summary)

            # Check if supervisor should intervene (every 3 failed attempts)
            history = json.loads(fix_history_path.read_text())
            attempt_count = len(history["attempts"])
            if attempt_count > 0 and attempt_count % 8 == 0:
                if not step_supervise(report_path, fix_history_path):
                    print("\nPipeline stopped: Supervisor failed")
                    return False

                if not apply_supervisor_guidance(fix_history_path, report_path):
                    print("\nSupervisor recommends escalation — stopping pipeline")
                    return False

                supervisor_just_ran = True

            # Continue pipeline — skip tests since we just ran them
            skip_tests = True
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
