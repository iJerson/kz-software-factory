#!/usr/bin/env python3
"""Merge the verified task branches into the feature branch, without an agent.

    python3 integrate.py <run-dir> [--repo PATH] [--timeout SECONDS]

Reads from the run directory: STATE.json (integration worktree and branch, each task's
status and branch) and TASKS.md (dependencies and the `Suite command:` line). Works only
inside the integration worktree. It never touches the base branch, never pushes, and
never edits code.

For each verified task, in dependency order:
  * already merged                -> skipped (so the script can be re-run safely)
  * a dependency was not merged   -> skipped, with the reason
  * merge conflict                -> the merge is aborted, the worktree is left clean, and
                                     the script stops with status "needs-human". The
                                     dispatcher spawns `factory-integrator` for that one
                                     conflict, then runs this script again to continue.
  * clean merge                   -> the suite runs; if it fails the merge is reverted and
                                     the task is reported as integration-failed
After the last merge the full suite runs once more if it has not already passed on the
final commit.

Writes integration.md (and appends to integration.log.jsonl) in the run directory, and
prints one JSON object:
  {"status": "ok|needs-human|failed", "merged": [...], "already": [...], "conflicts": [...],
   "suite_failed": [...], "skipped": [...], "not_verified": [...], "final_suite": "pass|fail|not-run",
   "artifact": "<run-dir>/integration.md", "summary": "..."}

The suite command is one plain command (no shell operators). It comes from TASKS.md, which
the user approves at the plan gate, or is detected from the repo when TASKS.md has none.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_eval as fe  # noqa: E402


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr).strip()


def detect_suite(wt):
    """Best-effort suite command for a repo with no `Suite command:` line, or None."""
    def has(*names):
        return any(os.path.exists(os.path.join(wt, n)) for n in names)

    if has("gradlew"):
        return "./gradlew test"
    if has("pom.xml"):
        return "mvn -q test"
    if has("package.json"):
        try:
            with open(os.path.join(wt, "package.json")) as f:
                if (json.load(f).get("scripts") or {}).get("test"):
                    return "npm test"
        except (OSError, ValueError):
            pass
    if has("pytest.ini", "tox.ini", "conftest.py") or has("tests/conftest.py"):
        return "python3 -m pytest -q"
    if has("go.mod"):
        return "go test ./..."
    if has("Cargo.toml"):
        return "cargo test"
    if has("tests") or has("test"):
        return "python3 -m unittest"
    return None


def run_suite(wt, cmd, timeout):
    """Returns (passed, seconds, output_tail)."""
    start = datetime.datetime.now()
    try:
        p = subprocess.run(shlex.split(cmd), cwd=wt, capture_output=True, text=True, timeout=timeout)
        ok, out = p.returncode == 0, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        ok, out = False, "suite timed out after %d seconds" % timeout
    except OSError as e:
        ok, out = False, "could not run the suite command: %s" % e
    secs = (datetime.datetime.now() - start).total_seconds()
    return ok, round(secs, 1), "\n".join(out.strip().splitlines()[-15:])


def order_tasks(tasks, verified):
    """Verified tasks in dependency order (ties broken by id)."""
    byid = {t["id"]: t for t in tasks if t["id"] in verified}
    done, out = set(), []
    pending = sorted(byid)
    while pending:
        ready = [t for t in pending if all(d in done or d not in byid for d in byid[t]["deps"])]
        if not ready:  # a cycle: fall back to id order so nothing is lost
            ready = pending[:1]
        for t in ready:
            out.append(byid[t])
            done.add(t)
            pending.remove(t)
    return out


def render(run_dir, c, suite, rows, final, notes):
    L = ["# Integration", "",
         "Branch: `%s` (worktree `%s`)" % (c.integ_branch, c.integ_worktree),
         "Suite command: `%s`" % (suite or "none"), "",
         "| Task | Result | Detail |", "|---|---|---|"]
    for tid, (result, detail) in rows.items():
        L.append("| %s | %s | %s |" % (tid, result, detail.replace("|", "/").replace("\n", " ")[:200]))
    reverted = [t for t, (r, _) in rows.items() if r == "integration-failed"]
    conflicts = [t for t, (r, _) in rows.items() if r == "conflict"]
    if conflicts:
        L += ["", "## Conflicts", ""] + ["- %s: %s" % (t, rows[t][1]) for t in conflicts]
    if reverted:
        L += ["", "## Reverted", "",
              "These merges broke the suite and were reverted: %s." % ", ".join(reverted)]
    if notes:
        L += ["", "## Conflict resolution notes", "", notes.strip()]
    L += ["", "Merge order: %s." % ", ".join(rows.keys()),
          "Final suite: %s" % final]
    path = os.path.join(run_dir, "integration.md")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--repo")
    ap.add_argument("--timeout", type=int, default=900, help="seconds allowed for each suite run")
    a = ap.parse_args()
    run_dir = os.path.abspath(a.run_dir)
    repo = os.path.abspath(a.repo) if a.repo else os.path.abspath(os.path.join(run_dir, "..", "..", ".."))
    c = fe.build_ctx(run_dir, repo)
    wt = os.path.join(repo, c.integ_worktree)
    log_path = os.path.join(run_dir, "integration.log.jsonl")
    result = {"status": "ok", "merged": [], "already": [], "conflicts": [], "suite_failed": [],
              "skipped": [], "not_verified": [], "final_suite": "not-run",
              "artifact": os.path.join(run_dir, "integration.md")}

    def fail(msg):
        result.update(status="failed", summary=msg)
        print(json.dumps(result))
        sys.exit(0)

    if not os.path.isdir(wt):
        fail("integration worktree %s does not exist" % wt)
    rc, head_branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or head_branch != c.integ_branch:
        fail("the integration worktree is on %r, expected %r" % (head_branch, c.integ_branch))
    suite = c.suite_command or detect_suite(wt)
    if not suite:
        fail("no suite command: add a 'Suite command: `...`' line to TASKS.md")
    if fe.SHELL_OPERATORS.search(suite):
        fail("the suite command %r has shell operators; use one plain command" % suite)

    states = c.state.get("tasks") or {}
    verified = {t for t, v in states.items() if (v or {}).get("status") == "verified"}
    result["not_verified"] = sorted(t for t in (c.task_ids or []) if t not in verified)
    todo = order_tasks(c.tasks, verified)
    rows, merged = {}, set()
    last_suite_sha = None

    def record(tid, res, detail):
        rows[tid] = (res, detail)
        with open(log_path, "a") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                "task": tid, "result": res, "detail": detail[:300]}) + "\n")

    for t in todo:
        tid = t["id"]
        branch = (states.get(tid) or {}).get("branch") or "factory/%s/%s" % (c.run_id, tid)
        if not fe.branch_exists(repo, branch):
            record(tid, "skipped", "branch %s does not exist" % branch)
            result["skipped"].append(tid)
            continue
        if git(wt, "merge-base", "--is-ancestor", branch, "HEAD")[0] == 0:
            record(tid, "already-merged", "branch is already in %s" % c.integ_branch)
            result["already"].append(tid)
            merged.add(tid)
            continue
        missing = [d for d in t["deps"] if d not in merged and d in verified]
        unverified = [d for d in t["deps"] if d not in verified]
        if missing or unverified:
            record(tid, "skipped", "depends on %s, which is not merged" % ", ".join(missing + unverified))
            result["skipped"].append(tid)
            continue
        before = git(wt, "rev-parse", "HEAD")[1]
        rc, out = git(wt, "merge", "--no-ff", "-m", "merge %s: %s" % (tid, t["title"] or tid), branch)
        if rc != 0:
            files = git(wt, "diff", "--name-only", "--diff-filter=U")[1].splitlines()
            git(wt, "merge", "--abort")
            if files:
                record(tid, "conflict", "conflicts in %s" % ", ".join(files))
                result["conflicts"].append({"task": tid, "branch": branch, "files": files})
            else:
                record(tid, "merge-failed", out.splitlines()[-1] if out else "git merge failed")
                result["conflicts"].append({"task": tid, "branch": branch, "files": []})
            break  # stop here; the dispatcher resolves this one, then re-runs the script
        ok, secs, tail = run_suite(wt, suite, a.timeout)
        if ok:
            record(tid, "merged", "suite passed in %ss" % secs)
            result["merged"].append(tid)
            merged.add(tid)
            last_suite_sha = git(wt, "rev-parse", "HEAD")[1]
        else:
            git(wt, "reset", "--hard", before)
            record(tid, "integration-failed", "the suite failed after the merge, so it was reverted: %s" % tail)
            result["suite_failed"].append(tid)

    if result["conflicts"]:
        stopped = result["conflicts"][0]["task"]
        for t in todo:
            if t["id"] not in rows:
                record(t["id"], "not-attempted", "stopped at the conflict in %s" % stopped)

    head = git(wt, "rev-parse", "HEAD")[1]
    if not result["conflicts"]:
        if last_suite_sha == head:
            result["final_suite"] = "pass"
        else:
            ok, secs, tail = run_suite(wt, suite, a.timeout)
            result["final_suite"] = "pass" if ok else "fail"
            if not ok:
                record("(final)", "suite-failed", tail)

    notes = ""
    notes_path = os.path.join(run_dir, "integration.notes.md")
    if os.path.exists(notes_path):
        with open(notes_path) as f:
            notes = f.read()
    final_line = {"pass": "pass", "fail": "fail", "not-run": "not run (stopped at a conflict)"}[result["final_suite"]]
    render(run_dir, c, suite, rows, final_line, notes)

    problems = bool(result["conflicts"] or result["suite_failed"] or result["skipped"]) \
        or result["final_suite"] == "fail"
    result["status"] = "needs-human" if problems else "ok"
    bits = ["merged %d" % len(result["merged"])]
    if result["already"]:
        bits.append("%d already merged" % len(result["already"]))
    if result["conflicts"]:
        bits.append("conflict in %s" % result["conflicts"][0]["task"])
    if result["suite_failed"]:
        bits.append("reverted %s" % ", ".join(result["suite_failed"]))
    if result["skipped"]:
        bits.append("skipped %s" % ", ".join(result["skipped"]))
    bits.append("final suite %s" % result["final_suite"])
    result["summary"] = ", ".join(bits)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
