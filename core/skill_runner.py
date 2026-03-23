"""Skill runner for executing Claude skills."""

import os
import subprocess
import threading
from pathlib import Path

from core.run_report import RunReport
from core.fix_history import FixHistory


class SkillRunner:
    """Runs Claude skills as subprocesses."""

    # Skills that need access to the target project
    PROJECT_SKILLS = ("test-replicator", "trace-analyzer", "bug-fixer", "fix-applier", "fix-supervisor", "commit-fixes")

    # Skills that need fix history
    FIX_HISTORY_SKILLS = ("trace-analyzer", "bug-fixer", "fix-supervisor")

    # Skills that can use supervisor guidance
    GUIDANCE_SKILLS = ("trace-analyzer", "bug-fixer")

    def __init__(self, root_dir: Path, skills_dir: Path):
        """Initialize the skill runner.

        Args:
            root_dir: Root directory of the pipeline
            skills_dir: Directory containing skill files
        """
        self._root_dir = root_dir
        self._skills_dir = skills_dir

    def run(self, skill_name: str, timeout: int = 7200, cwd: str = None,
            report: RunReport = None, fix_history: FixHistory = None) -> bool:
        """Run a Claude skill and return success/failure."""
        skill_path = self._skills_dir / f"{skill_name}.skill.md"

        if not skill_path.exists():
            print(f"  ERROR: Skill not found: {skill_path}")
            return False

        skill_content = skill_path.read_text()
        work_dir = cwd or str(self._root_dir)

        prompt = self._build_prompt(skill_name, report, fix_history)
        cmd = self._build_command(skill_name, skill_content, prompt)

        # Log skill start
        if report:
            report.log(skill_name, "started", f"Pipeline launched {skill_name}")

        # Start tailing the report file
        stop_event = None
        tail_thread = None
        if report:
            stop_event = threading.Event()
            tail_thread = threading.Thread(target=report.tail, args=(stop_event,), daemon=True)
            tail_thread.start()

        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                timeout=timeout,
                stdin=subprocess.DEVNULL
            )
            success = result.returncode == 0
            if report:
                status = "success" if success else "failed"
                report.log(skill_name, "finished", f"{skill_name} {status} (exit code {result.returncode})")
            return success

        except subprocess.TimeoutExpired:
            print(f"  ERROR: Skill timed out after {timeout}s")
            if report:
                report.log(skill_name, "error", f"Timed out after {timeout}s")
            return False
        except FileNotFoundError:
            print("  ERROR: Claude CLI not found")
            if report:
                report.log(skill_name, "error", "Claude CLI not found")
            return False
        finally:
            if stop_event:
                stop_event.set()
            if tail_thread:
                tail_thread.join(timeout=2)

    def _build_prompt(self, skill_name: str, report: RunReport = None,
                      fix_history: FixHistory = None) -> str:
        """Build the prompt for a skill."""
        prompt = "Execute the task defined in the system prompt."

        # Add target project dir for skills that need project access
        project_path = os.getenv("PROJECT_PATH")
        if skill_name in self.PROJECT_SKILLS and project_path:
            prompt += f" The target project is at: {project_path}"

        # Pass test command to test-replicator
        if skill_name == "test-replicator":
            execute_command = os.getenv("EXECUTE_COMMAND", "npm test")
            prompt += f" Execute command: {execute_command}"

        # Add fix history path
        if skill_name in self.FIX_HISTORY_SKILLS and fix_history:
            prompt += f" Fix history file: {fix_history.path}"

        # Add guidance path if supervisor has written one
        if skill_name in self.GUIDANCE_SKILLS:
            guidance_path = self._root_dir / "artifacts" / "strategy" / "guidance.json"
            if guidance_path.exists():
                prompt += f" Supervisor guidance file: {guidance_path}"

        # Add report path
        if report:
            prompt += f" Run report file: {report.path}"

        return prompt

    def _build_command(self, skill_name: str, skill_content: str, prompt: str) -> list:
        """Build the command to run a skill."""
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--model", "claude-opus-4-5",
            "--add-dir", str(self._root_dir),
        ]

        project_path = os.getenv("PROJECT_PATH")
        if skill_name in self.PROJECT_SKILLS and project_path:
            cmd.extend(["--add-dir", project_path])

        cmd.extend(["--system-prompt", skill_content, prompt])

        return cmd
