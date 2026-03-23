"""Pipeline orchestrator for the RootCause AI fix flow."""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.run_report import RunReport
from core.fix_history import FixHistory, extract_error_summary
from core.skill_runner import SkillRunner


class Pipeline:
    """Orchestrates the full bug detection and fixing flow."""

    def __init__(self, root_dir: Path, skills_dir: Path):
        """Initialize the pipeline.

        Args:
            root_dir: Root directory of the pipeline
            skills_dir: Directory containing skill files
        """
        self._root_dir = root_dir
        self._skills_dir = skills_dir
        self._artifacts_dir = root_dir / "artifacts"

        # These are set during run()
        self.report: RunReport = None
        self.fix_history: FixHistory = None
        self.skill_runner: SkillRunner = None

    def run(self) -> bool:
        """Run the full pipeline with loop support."""
        print("=" * 50)
        print("RootCause AI Pipeline")
        print("=" * 50)

        # Archive previous run artifacts before starting
        self._archive_previous_artifacts()

        # Create a new branch for this fix attempt
        print("\nCreating fix branch...")
        branch_name = self._create_fix_branch()
        if not branch_name:
            print("Pipeline stopped: Could not create fix branch")
            return False

        self.report = RunReport(self._artifacts_dir)
        print(f"Run report: {self.report.path}")

        self.fix_history = FixHistory(self._artifacts_dir, self.report.session_name)
        print(f"Fix history: {self.fix_history.path}")

        self.skill_runner = SkillRunner(self._root_dir, self._skills_dir)

        # Clear stale supervisor guidance from previous runs
        guidance_path = self._artifacts_dir / "strategy" / "guidance.json"
        if guidance_path.exists():
            guidance_path.unlink()
            print("Cleared stale supervisor guidance")

        skip_tests = False
        supervisor_just_ran = False

        while True:
            # Step 1: Run tests (skipped if we just ran them in fix_and_rerun)
            if not skip_tests:
                success, all_passed = self._step_run_tests()
                if not success:
                    print("\nPipeline stopped: Test execution failed")
                    return False

                # If all tests passed on first run, no fixes needed
                if all_passed:
                    print("\n✓ All tests passed!")
                    self._step_notify_success()
                    return True
            skip_tests = False

            # Step 2: Capture DOM (skip if failing line is same as previous attempt)
            should_replicate = True
            logs_dir = self._artifacts_dir / "rootcause_logs"
            log_files = sorted(logs_dir.glob("run_*.log"), key=lambda f: f.stat().st_mtime)
            if log_files:
                current_error = extract_error_summary(log_files[-1])
                history = self.fix_history.read()
                if history["attempts"]:
                    prev_error = history["attempts"][-1].get("error_after", "")
                    if current_error and prev_error and current_error == prev_error:
                        print("\n[2/6] Skipping DOM capture — same failing line as previous attempt")
                        should_replicate = False

            if should_replicate:
                if not self._step_replicate():
                    print("\nPipeline stopped: DOM capture failed")
                    return False

            # Step 3: Analyze
            if not self._step_analyze():
                print("\nPipeline stopped: Analysis failed")
                return False

            # Step 4: Generate fix
            if not self._step_generate_fix():
                print("\nPipeline stopped: Fix generation failed")
                return False

            # Step 5: Notify and get user action
            action = self._step_notify()

            # If supervisor just intervened, send a note about it
            if supervisor_just_ran:
                self._notify_supervisor_intervention()
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
                if not self._step_apply_fix():
                    print("\nPipeline stopped: Fix application failed")
                    return False

                # Run tests after fix
                print("\nFix applied. Rerunning tests...")
                success, all_passed = self._step_run_tests()
                if not success:
                    self.fix_history.record_attempt("failed", "Test execution failed")
                    print("\nPipeline stopped: Test execution failed")
                    return False

                if all_passed:
                    self.fix_history.record_attempt("passed")
                    print("\n✓ All tests passed after fix!")
                    self._step_commit_fixes()
                    self._step_notify_success()
                    return True

                # Tests still failing — record attempt with error summary
                logs_dir = self._artifacts_dir / "rootcause_logs"
                log_files = sorted(logs_dir.glob("run_*.log"), key=lambda f: f.stat().st_mtime)
                error_summary = extract_error_summary(log_files[-1]) if log_files else ""
                self.fix_history.record_attempt("failed", error_summary)

                # Check if supervisor should intervene (every 8 failed attempts)
                history = self.fix_history.read()
                attempt_count = len(history["attempts"])
                if attempt_count > 0 and attempt_count % 8 == 0:
                    if not self._step_supervise():
                        print("\nPipeline stopped: Supervisor failed")
                        return False

                    if not self._apply_supervisor_guidance():
                        print("\nSupervisor recommends escalation — stopping pipeline")
                        return False

                    supervisor_just_ran = True

                # Continue pipeline — skip tests since we just ran them
                skip_tests = True
                continue

            # Unknown action
            print(f"\nUnknown action: {action}")
            return False

    # -------------------------------------------------------------------------
    # Pipeline Steps
    # -------------------------------------------------------------------------

    def _step_run_tests(self) -> tuple[bool, bool]:
        """Run the test suite. Returns (success, all_passed)."""
        print("\n[1/6] Running tests...")

        # Import here to avoid circular issues
        sys.path.insert(0, str(self._root_dir))
        from core.project_runner import ProjectRunner

        project_path = os.getenv("PROJECT_PATH")
        command = os.getenv("EXECUTE_COMMAND", "npm test")

        if not project_path:
            print("  ERROR: PROJECT_PATH not set in .env")
            return False, False

        try:
            if self.report:
                self.report.log("pipeline", "step", f"Running tests: {command}")
            runner = ProjectRunner(project_path, command)
            log_file, exit_code, _ = runner.run()
            print(f"  Log: {log_file}")
            print(f"  Exit code: {exit_code}")
            if self.report:
                self.report.log("pipeline", "step", f"Tests finished. Exit code: {exit_code}. Log: {log_file}")
            all_passed = exit_code == 0
            return True, all_passed
        except Exception as e:
            print(f"  ERROR: {e}")
            if self.report:
                self.report.log("pipeline", "error", f"Test execution error: {e}")
            return False, False

    def _step_replicate(self) -> bool:
        """Capture DOM at failure point using test-replicator skill."""
        print("\n[2/6] Capturing DOM...")
        return self.skill_runner.run("test-replicator", timeout=3600, report=self.report)

    def _step_analyze(self) -> bool:
        """Analyze test failure using trace-analyzer skill."""
        print("\n[3/6] Analyzing failure...")
        return self.skill_runner.run("trace-analyzer", timeout=3600, report=self.report, fix_history=self.fix_history)

    def _step_generate_fix(self) -> bool:
        """Generate fix using bug-fixer skill."""
        print("\n[4/6] Generating fix...")
        return self.skill_runner.run("bug-fixer", timeout=3600, report=self.report, fix_history=self.fix_history)

    def _step_notify_success(self):
        """Send Telegram notification that all tests passed."""
        print("\nNotifying user: All tests passed!")

        from messaging.telegram_manager import TelegramManager

        tm = TelegramManager()
        tm.send_message("✅ All tests passed! No issues found.")

    def _step_notify(self) -> str:
        """Send Telegram notification and wait for user response."""
        print("\n[5/6] Sending notification...")

        from messaging.bugfix_notifier import BugFixMessageBuilder
        from messaging.telegram_manager import TelegramManager

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
            suggestions_dir = self._artifacts_dir / "suggestions"
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

    def _step_apply_fix(self) -> bool:
        """Apply fix using fix-applier skill."""
        print("\n[6/6] Applying fix...")
        return self.skill_runner.run("fix-applier", timeout=3600, report=self.report)

    def _step_commit_fixes(self) -> bool:
        """Commit all fixes using commit-fixes skill."""
        print("\nCommitting fixes...")
        return self.skill_runner.run("commit-fixes", timeout=600, report=self.report)

    def _step_supervise(self) -> bool:
        """Run the fix supervisor to analyze failure patterns and redirect."""
        print("\n[SUPERVISOR] Analyzing fix history...")
        return self.skill_runner.run("fix-supervisor", timeout=3600, report=self.report, fix_history=self.fix_history)

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _create_fix_branch(self) -> str:
        """Create a new branch for this fix attempt. Returns branch name or empty string on failure."""
        project_path = os.getenv("PROJECT_PATH")
        if not project_path:
            print("  ERROR: PROJECT_PATH not set")
            return ""

        # Extract test name from command
        execute_command = os.getenv("EXECUTE_COMMAND", "")
        test_name = self._extract_test_name_from_command(execute_command)
        test_name = self._sanitize_branch_name(test_name)

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

    def _extract_test_name_from_command(self, command: str) -> str:
        """Extract test name from EXECUTE_COMMAND for branch naming."""
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

    def _sanitize_branch_name(self, name: str) -> str:
        """Sanitize a string to be a valid git branch name."""
        # Replace spaces and invalid chars with hyphens
        sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', name)
        # Remove consecutive hyphens
        sanitized = re.sub(r'-+', '-', sanitized)
        # Remove leading/trailing hyphens
        sanitized = sanitized.strip('-')
        return sanitized.lower() or "test"

    def _archive_previous_artifacts(self):
        """Archive artifacts from a previous run before starting a new one."""
        # Items that indicate a previous run exists
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

        # Check if there's anything to archive
        has_artifacts = any((self._artifacts_dir / item).exists() for item in items_to_archive)
        if not has_artifacts:
            return

        # Try to get session name from existing fix_history.json
        fix_history_path = self._artifacts_dir / "fix_history.json"
        session_name = None
        if fix_history_path.exists():
            try:
                history = json.loads(fix_history_path.read_text())
                session_name = history.get("session")
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback to timestamp if no session name found
        if not session_name:
            session_name = f"run_{time.strftime('%Y-%m-%d_%H-%M-%S')}_archived"

        archive_base = self._root_dir / "previous_artifacts"
        archive_base.mkdir(parents=True, exist_ok=True)

        run_archive = archive_base / session_name
        run_archive.mkdir(exist_ok=True)

        for item in items_to_archive:
            src = self._artifacts_dir / item
            if src.exists():
                dst = run_archive / item
                if src.is_file():
                    shutil.move(str(src), str(dst))
                else:
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    shutil.rmtree(src)

        print(f"Archived previous artifacts to: {run_archive}")

    def _apply_supervisor_guidance(self) -> bool:
        """Read supervisor guidance and apply git reset if needed. Returns False if escalating."""
        guidance_path = self._artifacts_dir / "strategy" / "guidance.json"
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
            if self.report:
                self.report.log("pipeline", "supervisor_escalate", guidance.get("analysis", ""))
            return False

        # Apply git reset if rewind_commit is specified
        rewind_commit = guidance.get("rewind_commit")
        if not rewind_commit:
            print("  Supervisor provided guidance but no rewind needed")
            return True

        # Safety check: verify rewind_commit is at or after base_commit
        history = self.fix_history.read()
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
                if self.report:
                    self.report.log("pipeline", "supervisor_safety_block",
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
            if self.report:
                self.report.log("pipeline", "error", f"Git reset failed: {result.stderr}")
            return True

        print(f"  Rewound successfully. Direction: {guidance.get('direction', 'N/A')}")
        if self.report:
            self.report.log("pipeline", "supervisor_rewind",
                            f"Reset to {rewind_commit[:8]}. Direction: {guidance.get('direction', '')}")

        # Track intervention count
        self.fix_history.increment_supervisor_interventions()

        return True

    def _notify_supervisor_intervention(self):
        """Send notification about supervisor intervention."""
        from messaging.telegram_manager import TelegramManager

        guidance_path = self._artifacts_dir / "strategy" / "guidance.json"
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
