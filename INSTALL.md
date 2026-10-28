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
    python3 .claude/skills/software-factory/scripts/selftest_integrate.py
    python3 .claude/skills/software-factory/scripts/selftest_report.py

It builds a synthetic run, confirms it scores 100, then breaks it in specific ways and confirms the right check catches each one. It needs only `python3` and `git`.

## Merging

The factory never merges into your base branch, never offers to, and never pushes. When a run finishes it leaves the result on `feature/<slug>` and prints the commands to review and merge it. A merge happens only if you ask for one in your own words.

## Reviewers

Review is split into `factory-reviewer-ui`, `factory-reviewer-backend` and `factory-reviewer-security`. The dispatcher runs `scripts/route_reviewers.py` on each task's diff and spawns only the reviewers the changes need, in parallel: a backend-only change gets no UI reviewer, and a docs-only change gets none. The second self-test above checks the routing rules. The patterns live in that one script, so if the router spawns too much or too little for your codebase, tune it there (UI directories, security paths and patterns).

## Keeping the token cost down

Four changes cut the number of agent runs a task costs:

- **Fewer, larger tasks.** The planner is told to merge tasks that edit the same files, to keep code, tests and small supporting changes together, and to avoid tasks under about 20 changed lines. It states why there are this many tasks, and the eval flags tasks that were tiny or sequenced only because they share files.
- **Review is skipped for small, low-risk changes.** In review mode `auto` (the default), a task that is not high risk, has no security trigger and changed fewer than 30 lines gets no reviewer. Put `Review mode: always` in the request to review everything.
- **Merging is a script.** `integrate.py` merges the verified branches in dependency order, runs your suite after each merge, and reverts a merge that breaks it. An agent runs only to resolve a conflict.
- **The report is a script.** `make_report.py` writes `REPORT.md` from the artifacts. A reporter agent runs only if you ask for a summary.

The suite command comes from the plan (a `Suite command:` line), which you approve at Gate 2, and the script runs it for you. It must be one plain command with no `;`, `&&`, `|`, `>` or backticks. If your suite needs several steps, put them in a build-tool task or a script in the repo and name that.
