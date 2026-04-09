#!/bin/bash

cd /home/rwurmbra/Desktop/projects/fix-pilot

# Delete old review
rm -f artifacts/senior_review.json

# Run senior-reviewer exactly like the pipeline does
claude --dangerously-skip-permissions --model claude-opus-4-5 \
  --add-dir /home/rwurmbra/Desktop/projects/fix-pilot \
  --add-dir /home/rwurmbra/Desktop/projects/tackle2-ui/cypress \
  --system-prompt "$(cat skills/senior-reviewer.skill.md)" \
  "Execute the task defined in the system prompt. The target project is at: /home/rwurmbra/Desktop/projects/tackle2-ui/cypress Run report file: /home/rwurmbra/Desktop/projects/fix-pilot/artifacts/run_reports/run_2026-04-06_22-09-30.jsonl"
