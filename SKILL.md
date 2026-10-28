---
name: software-factory
description: Run the software factory pipeline for ONE feature or change, with one dedicated agent per phase (spec, design/plan, build, verify, review, integrate, report). Invoke ONLY when the user explicitly calls /software-factory, /software-factory resume [run-id], /software-factory eval [run-id], or says "run the factory", "start a factory run", or "factory: <request>". Never start on your own, never pick up tasks from a ledger unprompted, and never loop in the background.
disable-model-invocation: true
---

# Software Factory

A single, bounded pipeline run. It starts when the user invokes this skill and ends when the run is reported and applied (or abandoned). Nothing runs between invocations. Do not poll TASKS.md or resume old runs unless the user passes `resume`.

## Roles

This skill is the **dispatcher**. It runs in the main session and does only these things: collect inputs, spawn the phase agents, hold the human gates, keep the log and the run state, and manage worktrees. It does not merge into the base branch. It never writes product code or runs verification itself.

Each phase is its own subagent (definitions in `agents/`, copied to `.claude/agents/`):

| Phase | Agent | Model | Writes |
|---|---|---|---|
| 1 Spec | `factory-spec` | sonnet | SPEC.md |
| 2 Design + Plan (one gate; design runs by default) | `factory-planner`, `mode: design` then `mode: plan` | opus | DESIGN.md, TASKS.md |
| 3 Build | `factory-builder` (one per task, parallel) | per task | code on its own branch, `tasks/<id>/result.md` |
| 4 Verify | `factory-verifier` (one per task, never the builder) | sonnet | `tasks/<id>/verdict.json` |
| 4b Review | `factory-reviewer-ui`, `factory-reviewer-backend`, `factory-reviewer-security`: only the ones the task's changes need, in parallel | sonnet; opus for backend and security when risk is high | `tasks/<id>/review.<kind>.json` |
| 5 Integrate | `factory-integrator` | sonnet | `integration.md` |
| 6 Report | `factory-reporter` | haiku | REPORT.md |
| 7 Learn (only if asked) | `factory-learner` | sonnet | `.factory/proposals/*.md` |
| 8 Eval (on by default) | `scripts/factory_eval.py`, then `factory-evaluator` | sonnet | `eval.json`, `EVAL.md`, `eval_judge.json` |

Why gates live here and not in the agents: subagents cannot talk to the user. Each agent does its job, writes its artifact, and returns one status line. The dispatcher turns that into a gate or the next spawn.

## Run directory layout

Everything for a run lives under `.factory/` in the repo root:

    .factory/runs/<run-id>/        INPUT.md, SPEC.md, DESIGN.md, TASKS.md, STATE.json,
                                   agent_log.jsonl, tasks/<id>/..., integration.md, REPORT.md,
                                   eval.json, EVAL.md, eval_judge.json
    .factory/worktrees/<run-id>/   <task-id>/  (task worktrees, removed right after integration)
    .factory/worktrees/<slug>/     the integration worktree (kept until the final decision)

### Naming

- **slug**: a short kebab-case name for the feature, taken from the goal (for example `todo-api` or `refund-endpoint`). It is the same slug as in the run-id. Never use a generic word such as `integration`, `merge`, `final` or `combined`.
- **Integration branch**: `feature/<slug>`, the branch a PR would be opened from. The user can override the whole name (see Inputs). If the branch already exists, append `-2`, `-3`, and so on, and tell the user.
- **Integration worktree**: `.factory/worktrees/<slug>`, named after the feature, not the run.
- **Task branches and worktrees**: `factory/<run-id>/<task-id>` and `.factory/worktrees/<run-id>/<task-id>`. These are internal and short-lived.

The integration branch lives under `feature/`, not `factory/`, on purpose: git cannot hold a branch `factory/<slug>` and a branch `factory/<slug>/T1` at the same time.

Worktrees live inside the repo directory so they are covered by the same file permissions as the rest of the run. At preflight, add `.factory/` to `.git/info/exclude` if it is not already there (local only; this does not touch tracked files or `.gitignore`).

## Agent contract

- Every agent is given the run directory path and only the inputs listed for its phase.
- Every agent ends its reply with exactly one line of JSON: `{"status":"ok|needs-human|failed","artifact":"<path>","summary":"<one sentence>","open":["..."]}`
- **Write fallback.** Include this rule in every spawn prompt: if a Write is denied or fails, do not stop. Put the complete artifact text in your reply before the JSON line, and return `status: failed` with the summary `write denied`. The dispatcher then writes the file itself from your reply.
- An agent may not mark its own work verified, change another phase's artifact, or touch the base branch.

## Log

The dispatcher is the only writer of `agent_log.jsonl`. This avoids parallel writers corrupting the file.

- Append one line **immediately** after every agent returns, whatever the outcome (ok, needs-human, failed, write denied), and before any gate or next spawn. Never batch log writes, and never skip an agent, including the integrator and the reporter.
- Fields: `ts, run, task, agent, model, event, status, retries, tokens`.
- `tokens`: use the usage figure the spawn result reports for that agent. If the result does not report one, write `null`. **Never write 0 for an unknown count**; a zero would be read as a real measurement. Agents do not report their own tokens. If the result reports usage in parts (input, output, cache read, cache creation), also record those parts verbatim under `usage`. Do not add them up yourself or invent a figure: the total the harness reports may count cached input differently from the usage view, and the parts let the number be reinterpreted later.
- **Late usage figures.** If a figure arrives after the return line was written, do not edit that line (the log is append-only). Append `{"ts", "run", "event": "tokens_correction", "agent", "task", "mode", "retries", "tokens"}` with the same `agent`, `task`, `mode` and `retries` as the return it belongs to. Always include `mode` on planner lines and `retries` on retried tasks, so the match is unambiguous. A correction is not a new agent run.
- **Event names.** Use exactly these: `return` (an agent finished), `tokens_correction`, `gate`, `discarded`, `route`, `cleanup`, `eval`, `run_end`.
- **Gate events.** One line per gate decision, with exactly these fields: `{"ts", "run", "event": "gate", "gate": "GATE 1" | "GATE 2" | "GATE 3" | "GATE blocked", "status": "approved" | "rejected" | "waiting" | "skipped", "note": "<what the user said or decided>"}`. Do not invent gate names (no `GATE 1/2`) and do not put the status under another field name (not `decision`). If one user message answers two gates, or answers a gate and changes a setting, log one event per decision.
- **Route events.** Before spawning reviewers, append `{"ts", "run", "event": "route", "task", "retries", "reviewers": [...], "reasons": {...}, "skipped": {...}}` with the router's answer. On each reviewer's `return` line, include `blockers` (the number of blocker findings it reported).
- **Discarded runs.** If an agent's output is thrown away, for example because the user skips design while the design agent is running, append `{"ts", "run", "event": "discarded", "agent", "task", "mode", "retries", "reason"}`, matched to its return the same way a token correction is. That is how wasted tokens get reported.
- After the final step, append a `run_end` line with `tokens_known_total` (each agent run's figure counted once, whether it came inline or from a correction) and `agents_without_tokens` (how many agent runs still have no figure). Tell the user that the known total undercounts, and ask once for the session total from Claude Code's usage view. If they give it, record it as `tokens_session_total` on the `run_end` line, as a plain integer such as `104000` (not text like "104k", and not an object). The reporter's own cost is never in the log it reads, so say so in the report.

## State and resume

The log says what happened. `STATE.json` says where the run is, so a run can be picked up after a crash, a closed session, or a corrupted context. Conversation context is not part of the state: everything needed to continue must be on disk.

`.factory/runs/<run-id>/STATE.json`:

```json
{
  "run": "20261026-1030-todo-api",
  "slug": "todo-api",
  "repo": "/abs/path/to/repo",
  "base_branch": "main",
  "base_sha": "<sha at run start>",
  "phase": "spec|design|plan|build|verify|review|integrate|report|apply|eval|done|aborted",
  "gate": {"name": "GATE 2", "status": "waiting|approved|rejected|none"},
  "design_ran": true,
  "budget": {"retries": 2, "parallel": 3},
  "integration": {"branch": "feature/<slug>", "worktree": ".factory/worktrees/<slug>", "status": "pending|merged|failed"},
  "tasks": {
    "T1": {"status": "pending|ready|building|verifying|reviewing|verified|blocked|skipped|integration-failed",
           "retries": 0, "reviewed_sha": "<branch tip when last reviewed>", "branch": "factory/<run-id>/T1", "worktree": ".factory/worktrees/<run-id>/T1",
           "last_agent": "factory-builder", "last_status": "ok"}
  },
  "updated": "<iso timestamp>"
}
```

- Write it atomically (write `STATE.json.tmp`, then rename). Update it **before** spawning an agent (set the task or phase to its in-flight status) and again **after** the agent returns and the log line is written. Also update it when a gate opens and when it closes.
- Saving the user's gate answers matters: record each approval, rejection, and edit instruction in the log as an `event: gate` line.

### Resume

Invoked as `/software-factory resume [run-id]`. With no run-id, list the runs under `.factory/runs/` (phase, gate, last update) and ask which one.

1. Read `STATE.json`, the tail of `agent_log.jsonl`, and the artifacts present in the run directory.
2. **Reconcile with reality before trusting the state:**
   - Base branch HEAD differs from `base_sha`: stop and ask (restart, or rebase the work).
   - A task branch exists but its worktree is missing: recreate the worktree from the branch.
   - A task or phase is marked in flight with no return logged (the session died mid-agent). Treat it as interrupted, not failed, and do not charge a retry:
     - builder: if the worktree has commits ahead of base, send the task to verify; otherwise rebuild it.
     - verifier or reviewer: re-run it (for reviewers, only those with no review file yet). They are read-only, so this is safe.
     - integrator: check the integration branch and `integration.md`. If merges are partly done, re-spawn it to continue from that point; if the state is unclear, ask.
   - An artifact exists on disk but the state is behind it: show it to the user and ask whether to accept it.
3. If `STATE.json` is missing or unreadable but the run directory exists, rebuild a best-effort state from the log and the artifacts, show the reconstruction, and ask the user to confirm it.
4. Summarize: phase, open gate, task table, what was interrupted, and what you propose to do next. **Wait for confirmation before spawning anything.** Subagents cannot be resumed; a fresh agent is spawned from the artifacts on disk.

## Preflight and inputs

Take the request from the invocation text. If any of these are missing, ask once, in a single message:

1. Goal: what should exist or behave differently when done
2. Repo and base branch
3. Acceptance criteria: how we will know it works
4. Constraints: things not to touch, tech or compatibility limits
5. Budget (optional): max retries per task (default 2), max parallel builders (default 3)
6. Design mode (optional): `always` (default), `auto`, or `never`. See Gate 1.
7. Eval mode (optional): `full` (default: scripted checks plus the judge agent), `fast` (scripted checks only, no extra tokens), or `off`. See Phase 8.
8. Integration branch name (optional): default `feature/<slug>`.

Preflight checks, before creating anything. If a hard check fails, stop and tell the user. Never stash, checkout, reset, or commit on the user's behalf without asking.

- **Base branch is checked out.** In a brand-new repo `HEAD` points at an unborn branch; see the base commit check below.
- **No uncommitted changes to tracked files**, staged or unstaged (`git diff --quiet && git diff --cached --quiet`). **Untracked files never block a run.** Worktrees are created from the base commit, so untracked files cannot affect them. Do not ask the user to commit, ignore, or move their own untracked files (`.claude/`, `.DS_Store`, editor folders, and so on). They only matter at apply time, and only if the integration branch adds a file at the same path; in that case git refuses the merge and you report it.
- **A base commit exists.** If the repo has no commits yet, ask once: "This repo has no commits yet. Create an empty root commit on `<branch>` to use as the base? It adds no files." On yes, run `git commit --allow-empty -m "chore: software-factory base"`. If git reports a missing `user.name` or `user.email`, stop and tell the user to set it. On no, stop.
- Record `base_sha` (the current HEAD, after the step above).
- `.factory/` is excluded locally (see Run directory layout).

Create `.factory/runs/<run-id>/` (run-id: `YYYYMMDD-HHMM-<slug>`), save the inputs as `INPUT.md`, and write the first `STATE.json`.

## Pipeline

### Phase 1: Spec
Spawn `factory-spec` with INPUT.md. It reads the repo and writes SPEC.md with testable acceptance criteria and every ambiguity marked `OPEN:`.

**GATE 1:** show the spec and the OPEN items. Wait for approval. If the user answers the OPEN items, re-spawn `factory-spec` with the answers.

At this gate, also state which design mode is in force, so the user can change it with one word (for example "skip design"):
- `always` (the default): design runs. Do not skip it on your own judgment, however small the spec looks.
- `auto`: recommend skipping it only when the acceptance criteria touch a single module and look like about three tasks or fewer; otherwise run it. The user confirms or overrides.
- `never`: skip it.

Say plainly that design starts as soon as the user approves, so this is the moment to change the mode. A skip decided later costs the tokens the design agent has already used.

### Phase 2: Design and plan (one gate)
Run the planner twice, back to back, then hold a single gate over both outputs.

1. **Design (runs unless the mode is `never`, or `auto` and the spec is small):** spawn `factory-planner` with `mode: design`, SPEC.md, and INPUT.md. It writes DESIGN.md: approach, interfaces and contracts, data model changes, key decisions with rejected alternatives, impact, and risks.
   - If it returns `needs-human` or lists OPEN items, stop here and ask the user before planning. Do not plan on top of an unresolved design.
2. **Plan:** spawn `factory-planner` with `mode: plan`, SPEC.md, and one line, `design: ran` or `design: skipped`. Give it the DESIGN.md path only when design ran. It writes TASKS.md: id, title, depends_on, files touched, mechanical definition of done, model tier, risk (`normal` or `high`), status. With a design, tasks are split along its interface boundaries.

**Sequencing.** Run the design agent and then the plan agent in the foreground, one after the other, and wait for each to return. Do not spawn the plan agent while the design agent is still running, unless the user has skipped design.

**A gate answer given before its artifact exists is not an approval.** If the user says "approve plan" while the design is still being written, treat it only as an instruction (for example, skip design), carry it out, produce the artifact, and then hold the gate as normal.

**If the user skips design while the design agent is already running:** stop that agent if the harness allows it; otherwise let it finish. Either way its output is discarded. After it returns, delete `DESIGN.md` if it wrote one, and do not rename or keep it under another name (no `DESIGN.skipped.md`). Log a `discarded` event for it, set `design_ran: false`, and tell the user in one line that the design run's tokens were already spent.

**When design is skipped** (mode `never`, or `auto` and the spec is small): do not spawn the planner in design mode, and **create no design file of any kind**: no `DESIGN.md`, no `DESIGN.skipped.md`, no empty or placeholder file. A file saying design was skipped costs tokens and means nothing. Record the skip only in `STATE.json` (`design_ran: false`) and in the note on the Gate 1 log event. Pass later agents no design path and do not mention a design to them.

**GATE 2:** show DESIGN.md (if it ran) and TASKS.md together, leading with the design's key decisions and the task list. Wait for approval. The user may edit either file first; re-read both before continuing. If the user rejects the design, restart from step 1 with their feedback; the plan is discarded and rebuilt. If they reject only the plan, re-run step 2 with their feedback.

### Phase 3: Build
For each ready task (dependencies verified), up to the parallel limit:
1. Create the worktree and branch: `git worktree add .factory/worktrees/<run-id>/<task-id> -b factory/<run-id>/<task-id> <base_branch>`. Record both in `STATE.json`.
2. Spawn `factory-builder` with: the task row, SPEC.md, the DESIGN.md path only if design ran, and its worktree path. Override the agent's model with the task's model tier. Do not hand builders the full ledger.

### Phase 4: Verify
When a builder returns, spawn a fresh `factory-verifier` on that task's branch. It writes `verdict.json` with `pass|fail`, a failure class (`spec-ambiguity | test-failure | env-error | wrong-approach | scope-creep`), and evidence.

- pass: move the task to review (Phase 4b).
- fail: spawn a new `factory-builder` with the verdict attached. Retries +1.
- retries exhausted, or class is `spec-ambiguity`: mark `blocked`.

### Phase 4b: Review
Only tasks that passed verification are reviewed. There are three specialist reviewers: `factory-reviewer-ui`, `factory-reviewer-backend` and `factory-reviewer-security`. **Spawn only the ones the task's changes need**, and run them in parallel.

1. **Route.** Run `python3 .claude/skills/software-factory/scripts/route_reviewers.py <repo> <base_sha> factory/<run-id>/<task-id> --risk <normal|high>`, taking the risk from the task row. It is deterministic and costs no tokens. It returns the reviewers needed, the files in each domain, and why each was or was not needed. Do not override it with your own judgement. If it returns no reviewers (only documentation or trivial files changed), mark the task `verified` with no review.
2. **Log the route.** Append a `route` event before spawning (see Log).
3. **Spawn in parallel.** Spawn all of the routed reviewers for the task in a single message, so they run at the same time. A task's reviewers always run together, even if that exceeds the parallel limit; the limit applies across tasks. Give each one: the task row (including its risk level), SPEC.md, the DESIGN.md path only if design ran, the branch, the base branch, and the files the router assigned to its domain. Give the security reviewer the router's reason for spawning it. Use sonnet for all of them, and override `backend` and `security` to opus when the task's risk is `high` (leave `ui` on sonnet). Each writes `tasks/<id>/review.<kind>.json`. Record the branch tip at this moment as `reviewed_sha` in `STATE.json`.
4. **Decide once every routed reviewer has returned.**
   - no blockers in any review: mark the task `verified`. Carry the should-fix and nit findings forward to the report.
   - one or more blockers: merge the blocker findings from all the reviews, spawn a new `factory-builder` with them attached, retries +1, then re-run Phase 4 on the result. Should-fix and nit findings are not sent back to builders.
   - retries exhausted: mark `blocked`.
5. **After a retry, re-review only what is needed.** Run the router again with `--since <reviewed_sha>`. Re-run every reviewer that reported a blocker, plus every reviewer the `--since` route returns, because their domain changed since they last looked. Do not re-run a reviewer that had no blocker and whose domain was untouched. Log a new `route` event with the new `retries`.

**GATE (on blocked):** show the task, failure class or blocker findings, and attempts. The user decides: edit spec, edit task, skip, or abort.

Phases 3, 4, and 4b repeat until every task is `verified`, `skipped`, or the run is aborted.

### Phase 5: Integrate
Create the integration worktree and branch, named after the feature (see Naming): `git worktree add .factory/worktrees/<slug> -b feature/<slug> <base_branch>`, and record both in `STATE.json`. Spawn `factory-integrator` with the integration worktree path, the branch name, and the verified branches in dependency order. It merges one at a time **inside that worktree**, re-runs the full suite after each merge, and reverts any merge that breaks it. Failures return as `needs-human` and become a gate like the one above. It never touches the base branch or the user's working directory.

### Phase 5b: Clean up task worktrees
The dispatcher does this itself, right after the integrator returns (and also when a run is blocked or aborted); do not leave it to an agent. It removes the **task** worktrees only. **The integration worktree stays.** It is the workspace for reviewing the result or opening a PR, and it is dealt with at Finish. On an abort, remove it as well.

1. For every worktree under `.factory/worktrees/<run-id>/`, check for uncommitted changes. If there are any, do not force-remove it: list them and ask the user.
2. Otherwise remove it: `git worktree remove <path>`. Then `git worktree prune`, and delete `.factory/worktrees/<run-id>/` if it is empty.
3. **Keep every branch.** The branches hold the work and are needed for review, merging, and retries. Branch deletion happens only at Finish, with the user's agreement.
4. Update `STATE.json` and log the cleanup (which task worktrees were removed, and that the integration worktree was kept).

### Phase 6: Report
Spawn `factory-reporter`. It writes REPORT.md: acceptance criteria met or not with evidence, tasks by outcome, retries used, tokens by model, integration branch, and follow-ups. Pass it the `agent_log.jsonl` for cost numbers. It also reads every `review.*.json` (and a legacy `review.json`, if an older run has one) so the remaining should-fix and nit findings reach you.

If the reporter returns `write denied` with the report text in its reply, write `REPORT.md` yourself from that text, then continue. Verify that the file exists before moving on.

### Gate 3: the final decision

**GATE 3:** show the summary, then say where the result is. **The factory never merges into the base branch on its own, and it never offers or recommends a merge.** The result stays on `feature/<slug>`, with its worktree at `.factory/worktrees/<slug>`. Print these commands for the user to run themselves; do not run them:

    git diff <base_branch>...feature/<slug>      # review the change
    git merge --ff-only feature/<slug>           # to merge locally, run from <base_branch>
    # or push feature/<slug> and open a PR

Then ask only whether to **keep** the result (the default, and what happens if the user does not say; log the decision note as `leave`) or **discard** it. The factory never pushes to a remote.

**A merge happens only on explicit request.** If, and only if, the user asks in their own words for the result to be merged into the base branch, do it as below (log the decision note as `apply`). Do not suggest it, and never treat approving Gate 3 as a request to merge.
1. Preflight: there are no uncommitted changes to tracked files, the base branch is checked out, and its HEAD is `base_sha` (or an ancestor of the feature branch). Untracked files are fine unless the merge would overwrite one; if git refuses for that reason, report which files and stop. If any of these fails, stop and tell the user; do not stash, checkout, or reset to make it work.
2. `git merge --ff-only feature/<slug>`. If it cannot fast-forward (the base moved), stop and offer: rebase the feature branch onto the new base and re-run the suite, or keep it for a PR.
3. Run the project's build and test command in the user's working directory and report the result. If it fails, say so plainly; do not revert without asking.

After the decision has been carried out (keep, discard, or an explicitly requested merge), go to Phase 8.

### Phase 8: Eval

Runs after the Gate 3 decision has been carried out and **before any branch is deleted**, because some checks read the task branches. It judges the agents and the dispatcher, not the product code: the acceptance tests and the verifier already cover the code. Skip it when the eval mode is `off`.

It can also be run on a finished run with `/software-factory eval [run-id]`. Checks that need a deleted branch are then reported as not applicable.

1. Set `phase: eval` in `STATE.json`.
2. Run the scripted checks: `python3 .claude/skills/software-factory/scripts/factory_eval.py <run-dir>`. This is deterministic and costs no tokens. It writes `eval.json` and `EVAL.md`.
3. Unless the mode is `fast`: get the rubric with `python3 .claude/skills/software-factory/scripts/factory_eval.py rubric`, spawn `factory-evaluator` with the run directory and that rubric, then re-run step 2 with `--judge <run-dir>/eval_judge.json` to blend the scores. The judge reads the whole run, so it costs tokens; `fast` skips it. If the judge fails or writes nothing, report the scripted score alone and say the judge did not run.
4. Show the user the verdict, the overall score, the per-agent table, and the **action items** (what to do in this run, and what to change in the factory), critical first. Do not soften or reinterpret them, and do not carry out an action item unless the user asks.
5. Log an `eval` event with `score`, `verdict`, and `critical_failures`.

### Finish
1. **Integration worktree.** After `apply` or `discard`, remove `.factory/worktrees/<slug>` (`git worktree remove`, with the same uncommitted-changes check as in Phase 5b, then `git worktree prune`). After `leave`, keep it, tell the user its path and the branch name to open the PR from, and tell them how to remove it when they are done: `git worktree remove .factory/worktrees/<slug>`.
2. **Branches.** Offer to delete them, and delete only on a yes. After `apply`: the task branches `factory/<run-id>/*` and `feature/<slug>`, with `git branch -d`. After `discard`: the same, with `-D`. After `leave`: keep everything.
3. Set `phase: done` in `STATE.json`, append the `run_end` log line (include `eval_score` and `eval_verdict` when an eval ran), and ask for the session token total (see Log).

### Phase 7: Learn (only if the user asks)
Spawn `factory-learner` with the log, all `verdict.json` files, and `eval.json` (including its action items) if an eval ran. It writes proposals only. Nothing is promoted automatically.

## Safety defaults

- Destructive actions (force push, deleting branches other than this run's (`factory/<run-id>/*` and its feature branch), touching files in the constraints list) need explicit user confirmation.
- Never push to a remote. Never modify the user's working directory or the base branch, except for a merge the user explicitly asked for in their own words.
- If the budget is exceeded (retries or tokens), stop and report rather than continue.
- If tracked files have uncommitted changes or the base branch moved unexpectedly at any point, stop and ask.
