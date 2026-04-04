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

    def run(self, autofix: bool = False) -> bool:
        """Run the full pipeline with loop support.

        Args:
            autofix: If True, skip Telegram approvals and auto-apply fixes.
        """
        self._autofix = autofix
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

        # Commentator diary (isolated from other skills)
        self._commentator_diary_dir = self._artifacts_dir / "commentator_diary"
        self._commentator_diary_dir.mkdir(parents=True, exist_ok=True)
        self._commentator_diary_path = self._commentator_diary_dir / "diary.json"

        # Cleaner tracking file
        self._cleaner_tracking_dir = self._artifacts_dir / "cleaner"
        self._cleaner_tracking_dir.mkdir(parents=True, exist_ok=True)
        self._cleaner_tracking_path = self._cleaner_tracking_dir / "tracking.json"

        skip_tests = False
        fix_attempted = False  # Track if a fix has been attempted

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
                    if not self._autofix:
                        self._step_notify_success()
                    return True
            skip_tests = False

            # Step 2: Copy screenshot, then capture DOM
            self._copy_screenshot()
            if not self._step_replicate():
                print("\nPipeline stopped: DOM capture failed")
                return False
            self._rename_screenshot_to_match_dom()

            # Step 3: Analyze
            if not self._step_analyze():
                print("\nPipeline stopped: Analysis failed")
                return False

            # Step 4: Generate fix
            if not self._step_generate_fix():
                print("\nPipeline stopped: Fix generation failed")
                return False

            # Step 5: Notify and get user action (skip if autofix)
            if self._autofix:
                action = "fix_and_rerun"
            else:
                action = self._step_notify()

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

                fix_attempted = True  # Mark that a fix has been attempted

                # Run tests after fix
                print("\nFix applied. Rerunning tests...")
                success, all_passed = self._step_run_tests()

                # Commentator observes after tests (only after fix was attempted)
                self._step_commentator()
                if not success:
                    self.fix_history.record_attempt("failed", "Test execution failed")
                    print("\nPipeline stopped: Test execution failed")
                    return False

                if all_passed:
                    self.fix_history.record_attempt("passed")
                    print("\n✓ All tests passed after fix!")

                    # Run cleaner loop
                    if not self._run_cleaner_loop():
                        print("\nPipeline stopped: Cleaner loop failed")
                        return False

                    # Analyze impact - find all tests that might be affected
                    self._step_impact_analysis()

                    self._step_commit_fixes()

                    # Senior review loop - refactor if needed
                    self._run_senior_review_loop()

                    if not self._autofix:
                        self._step_notify_success()
                    return True

                # Tests still failing — record attempt with error summary
                logs_dir = self._artifacts_dir / "rootcause_logs"
                log_files = sorted(logs_dir.glob("run_*.log"), key=lambda f: f.stat().st_mtime)
                error_summary = extract_error_summary(log_files[-1]) if log_files else ""
                self.fix_history.record_attempt("failed", error_summary)

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

    def _copy_screenshot(self):
        """Copy Cypress screenshot to artifacts before DOM capture."""
        project_path = os.getenv("PROJECT_PATH")
        if not project_path:
            return
        script = self._root_dir / "scripts" / "copy_cypress_screenshot.sh"
        try:
            subprocess.run(
                [str(script), project_path, str(self._artifacts_dir)],
                capture_output=True, text=True, timeout=30
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _rename_screenshot_to_match_dom(self):
        """Rename screenshot to match DOM snapshot filename."""
        script = self._root_dir / "scripts" / "rename_screenshot_to_match_dom.sh"
        try:
            subprocess.run(
                [str(script), str(self._artifacts_dir)],
                capture_output=True, text=True, timeout=30
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _step_replicate(self) -> bool:
        """Capture DOM at failure point using dom-capturer skill."""
        print("\n[2/6] Capturing DOM...")
        return self.skill_runner.run("dom-capturer", timeout=3600, report=self.report)

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
        """Commit all fixes using fix-committer skill."""
        print("\nCommitting fixes...")
        return self.skill_runner.run("fix-committer", timeout=600, report=self.report)

    def _step_commentator(self) -> bool:
        """Run commentator to observe and document in its private diary."""
        print("\n[Commentator] Observing...")
        return self.skill_runner.run(
            "commentator",
            timeout=300,
            report=self.report,
            fix_history=self.fix_history,
            diary_path=self._commentator_diary_path
        )

    def _step_cleaner(self) -> bool:
        """Run cleaner to remove unnecessary code from failed attempts."""
        print("\n[Cleaner] Cleaning...")
        return self.skill_runner.run(
            "cleaner",
            timeout=600,
            report=self.report,
            fix_history=self.fix_history,
            diary_path=self._commentator_diary_path,
            tracking_path=self._cleaner_tracking_path
        )

    def _run_cleaner_loop(self) -> bool:
        """Run cleaner loop: clean, test, restore if needed, repeat until pass."""
        print("\n[Cleaner] Starting cleanup loop...")

        while True:
            # Run cleaner
            if not self._step_cleaner():
                print("  Cleaner failed")
                return False

            # Run tests
            success, all_passed = self._step_run_tests()
            if not success:
                print("  Test execution failed during cleanup")
                return False

            if all_passed:
                print("  ✓ Tests still pass after cleanup")
                self._step_commit_cleanup()
                self._step_notify_cleanup_done()
                return True

            # Tests failed - cleaner will restore on next iteration
            print("  Tests failed after cleanup, cleaner will restore...")

    def _step_commit_cleanup(self) -> bool:
        """Commit cleanup changes."""
        print("\n[Cleaner] Committing cleanup...")
        return self.skill_runner.run("fix-committer", timeout=600, report=self.report)

    def _step_notify_cleanup_done(self):
        """Send Telegram notification that cleanup is done."""
        print("\nNotifying user: Cleanup complete!")

        from messaging.telegram_manager import TelegramManager

        tm = TelegramManager()
        if self._autofix:
            tm.send_message("✅ Autofix complete! Tests pass, cleanup done, changes committed.")
        else:
            tm.send_message("🧹 Cleanup complete! Unnecessary code removed and committed.")

    def _step_impact_analysis(self) -> bool:
        """Analyze which tests might be affected by the changes."""
        print("\n[Impact] Analyzing affected tests...")
        return self.skill_runner.run(
            "impact-analyzer",
            timeout=600,
            report=self.report,
            fix_history=self.fix_history
        )

    def _step_senior_review(self) -> bool:
        """Run senior reviewer to check fix quality."""
        print("\n[Senior] Reviewing fix...")
        return self.skill_runner.run(
            "senior-reviewer",
            timeout=900,
            report=self.report
        )

    def _run_senior_review_loop(self):
        """Run senior review loop: review, refactor if needed, test, repeat."""
        print("\n[Senior] Starting review loop...")

        project_path = os.getenv("PROJECT_PATH")
        if not project_path:
            print("  ERROR: PROJECT_PATH not set")
            return

        # Save safe commit to revert to if refactoring breaks tests
        safe_commit = self._get_head_commit(project_path)
        max_iterations = 3

        for iteration in range(max_iterations):
            print(f"\n[Senior] Review iteration {iteration + 1}/{max_iterations}")

            # Run senior reviewer
            if not self._step_senior_review():
                print("  Senior review failed")
                return

            # Read decision from artifact
            review_file = self._artifacts_dir / "senior_review.json"
            if not review_file.exists():
                print("  No review file produced, assuming approved")
                return

            review = json.loads(review_file.read_text())
            decision = review.get("decision", "approved")

            if decision == "approved":
                print("  ✓ Fix approved by senior reviewer")
                return

            if decision == "stopped":
                print("  Senior reviewer stopped: " + review.get("reason", ""))
                # Reset to safe commit if we made any changes
                if iteration > 0:
                    self._reset_to_commit(project_path, safe_commit)
                return

            if decision == "refactoring":
                print("  Refactoring applied, running tests...")

                # Run tests
                success, all_passed = self._step_run_tests()
                if not success:
                    print("  Test execution failed, reverting...")
                    self._reset_to_commit(project_path, safe_commit)
                    return

                if all_passed:
                    print("  ✓ Tests pass after refactoring")
                    self._step_commit_fixes()
                    safe_commit = self._get_head_commit(project_path)
                    # Continue loop for more review
                else:
                    print("  Tests failed after refactoring, reverting...")
                    self._reset_to_commit(project_path, safe_commit)
                    # Continue loop - reviewer will see failure and decide

        print(f"  Max iterations ({max_iterations}) reached")

    def _get_head_commit(self, project_path: str) -> str:
        """Get current HEAD commit hash."""
        try:
            result = subprocess.run(
                ["git", "-C", project_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    def _reset_to_commit(self, project_path: str, commit: str):
        """Reset project to a specific commit."""
        if not commit:
            return
        try:
            subprocess.run(
                ["git", "-C", project_path, "reset", "--hard", commit],
                capture_output=True, text=True, timeout=30
            )
            print(f"  Reset to commit {commit[:8]}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

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
            "suggestions",
            "fix_history.json",
            "dom_snapshots",
            "commentator_diary",
            "cleaner",
            "cleaner_report.json",
            "impact_analysis.json",
            "senior_review.json",
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
