# Install

From your repo root:

    mkdir -p .claude/skills/software-factory/scripts .claude/agents
    cp SKILL.md .claude/skills/software-factory/SKILL.md
    cp scripts/*.py .claude/skills/software-factory/scripts/
    cp agents/*.md .claude/agents/
    rm -f .claude/agents/factory-reviewer.md   # replaced by the three specialist reviewers

Then invoke with:

    /software-factory <your request>
    /software-factory resume [run-id]

The factory adds `.factory/` to `.git/info/exclude` itself (local only), so nothing is added to your `.gitignore`.

## Permissions

Subagents can't stop and ask you to approve a file write, so a write that isn't pre-approved can be denied mid-run (this is the most likely cause of a reporter whose Write was rejected). The dispatcher has a fallback and writes the file from the agent's reply, but it is better not to hit it. Allow edits under `.factory/` in `.claude/settings.json`, for example:

    {
      "permissions": {
        "allow": ["Edit(.factory/**)"]
      }
    }

The post-run eval runs a bundled script, so also allow it, for example `Bash(python3 .claude/skills/software-factory/scripts/*)`.

Check the exact rule syntax against the permissions docs for your Claude Code version. Builders also need to run your build and test commands (for example `Bash(./gradlew test:*)`), or the verifier will be denied the same way.

## What a run leaves behind

- `.factory/runs/<run-id>/`: spec, design, tasks, state, log, per-task verdicts and reviews, report. Safe to delete after the run.
- The feature branch `feature/<slug>` (named after your feature) and the task branches `factory/<run-id>/*`: kept until you agree to delete them at the end.
- Task worktrees are removed automatically after integration. The integration worktree `.factory/worktrees/<slug>` stays after the run so you can review the result or open a PR from it; remove it yourself when you are done with `git worktree remove .factory/worktrees/<slug>`, or choose discard at the end.

## Check the eval works

    python3 .claude/skills/software-factory/scripts/selftest_eval.py
    python3 .claude/skills/software-factory/scripts/selftest_route.py

It builds a synthetic run, confirms it scores 100, then breaks it in specific ways and confirms the right check catches each one. It needs only `python3` and `git`.

## Merging

The factory never merges into your base branch, never offers to, and never pushes. When a run finishes it leaves the result on `feature/<slug>` and prints the commands to review and merge it. A merge happens only if you ask for one in your own words.

## Reviewers

Review is split into `factory-reviewer-ui`, `factory-reviewer-backend` and `factory-reviewer-security`. The dispatcher runs `scripts/route_reviewers.py` on each task's diff and spawns only the reviewers the changes need, in parallel: a backend-only change gets no UI reviewer, and a docs-only change gets none. The second self-test above checks the routing rules. The patterns live in that one script, so if the router spawns too much or too little for your codebase, tune it there (UI directories, security paths and patterns).
