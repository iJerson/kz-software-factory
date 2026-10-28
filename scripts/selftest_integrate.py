#!/usr/bin/env python3
"""Self-test for integrate.py.

    python3 selftest_integrate.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import integrate  # noqa: E402

RUN = "20261026-1000-demo"
SLUG = "demo"
SUITE_LINE = "Suite command: `python3 suite.py`"


def sh(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def build(tmp, tasks, suite_line=SUITE_LINE):
    """tasks: [{id, deps, files: {path: text}, status}]. Creates a repo, the task
    branches, the integration worktree, STATE.json and TASKS.md."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.email", "t@example.com")
    sh(repo, "config", "user.name", "tester")
    put(os.path.join(repo, "suite.py"),
        "import os, sys\nsys.exit(1 if os.path.exists('BROKEN') else 0)\n")
    put(os.path.join(repo, "shared.txt"), "base\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "base")
    base = sh(repo, "rev-parse", "HEAD")
    for t in tasks:
        sh(repo, "checkout", "-q", "-b", "factory/%s/%s" % (RUN, t["id"]), base)
        for p, txt in t["files"].items():
            put(os.path.join(repo, p), txt)
        sh(repo, "add", "-A")
        sh(repo, "commit", "-q", "-m", "build " + t["id"])
        sh(repo, "checkout", "-q", "main")
    wt = os.path.join(repo, ".factory", "worktrees", SLUG)
    sh(repo, "worktree", "add", "-q", wt, "-b", "feature/" + SLUG, base)
    rd = os.path.join(repo, ".factory", "runs", RUN)
    rows = ["| id | title | depends_on | files likely touched | definition of done | model tier | risk | status |",
            "|---|---|---|---|---|---|---|---|"]
    for t in tasks:
        rows.append("| %s | Task %s | %s | `x` | `true` passes | sonnet | normal | %s |" % (
            t["id"], t["id"], ", ".join(t.get("deps", [])) or "-", t.get("status", "verified")))
    put(os.path.join(rd, "TASKS.md"), "# Tasks\n\n" + "\n".join(rows) + "\n\n" + suite_line + "\n")
    state = {"run": RUN, "slug": SLUG, "repo": repo, "base_branch": "main", "base_sha": base,
             "phase": "integrate",
             "integration": {"branch": "feature/" + SLUG, "worktree": ".factory/worktrees/" + SLUG},
             "tasks": {t["id"]: {"status": t.get("status", "verified"),
                                 "branch": "factory/%s/%s" % (RUN, t["id"])} for t in tasks}}
    put(os.path.join(rd, "STATE.json"), json.dumps(state))
    return repo, rd, wt, base


def run(rd):
    p = subprocess.run([sys.executable, os.path.join(HERE, "integrate.py"), rd],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def merges(wt):
    return sh(wt, "log", "--first-parent", "--format=%s").splitlines()


def clean_state(wt):
    return sh(wt, "status", "--porcelain") == "" and not os.path.exists(
        os.path.join(sh(wt, "rev-parse", "--git-dir"), "MERGE_HEAD")) and not os.path.exists(
        os.path.join(wt, ".git", "MERGE_HEAD"))


def main():
    failures = []

    def expect(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    tmp = tempfile.mkdtemp(prefix="integrate-selftest-")
    try:
        print("clean merge in dependency order")
        repo, rd, wt, base = build(os.path.join(tmp, "a"), [
            {"id": "T3", "deps": ["T1", "T2"], "files": {"app/c.py": "c=1\n"}},
            {"id": "T2", "files": {"app/b.py": "b=1\n"}},
            {"id": "T1", "files": {"app/a.py": "a=1\n"}}])
        r = run(rd)
        expect(r["status"] == "ok" and r["final_suite"] == "pass", "status ok, final suite pass (got %s, %s)" % (r["status"], r["final_suite"]))
        expect(r["merged"] == ["T1", "T2", "T3"], "merged in dependency order although listed in reverse (got %s)" % r["merged"])
        subjects = merges(wt)
        expect([s.split(":")[0] for s in subjects[:3]] == ["merge T3", "merge T2", "merge T1"],
               "feature branch history has the three merges in order (got %s)" % subjects[:3])
        md = open(os.path.join(rd, "integration.md")).read()
        expect(md.rstrip().splitlines()[-1] == "Final suite: pass" and "suite passed" in md,
               "integration.md records each suite run and ends with the final result")
        expect(sh(repo, "rev-parse", "main") == base, "the base branch was not touched")

        print("re-running is safe")
        head = sh(wt, "rev-parse", "HEAD")
        r = run(rd)
        expect(r["status"] == "ok" and r["already"] == ["T1", "T2", "T3"] and r["merged"] == [],
               "a second run merges nothing new (got %s)" % r["summary"])
        expect(sh(wt, "rev-parse", "HEAD") == head, "and creates no new commits")

        print("conflict: abort cleanly, stop, then resume after it is resolved")
        repo, rd, wt, base = build(os.path.join(tmp, "b"), [
            {"id": "T1", "files": {"shared.txt": "one\n"}},
            {"id": "T4", "files": {"shared.txt": "four\n"}},
            {"id": "T7", "files": {"app/g.py": "g=1\n"}}])
        r = run(rd)
        expect(r["status"] == "needs-human" and r["conflicts"] and r["conflicts"][0]["task"] == "T4"
               and r["conflicts"][0]["files"] == ["shared.txt"],
               "stops at the conflict and names the task and file (got %s)" % r["conflicts"])
        expect(r["merged"] == ["T1"] and clean_state(wt),
               "T1 stayed merged and the worktree is clean, with no merge in progress")
        md = open(os.path.join(rd, "integration.md")).read()
        expect("not-attempted" in md and "conflict" in md, "integration.md lists the conflict and the task it never reached")
        # what the integrator agent would do: resolve and commit the merge in the worktree
        sh(wt, "merge", "--no-ff", "-X", "ours", "-m", "resolve T4", "factory/%s/T4" % RUN)
        put(os.path.join(rd, "integration.notes.md"), "T4: kept T1's shared.txt, both edits were cosmetic.\n")
        r = run(rd)
        expect(r["status"] == "ok" and "T4" in r["already"] and r["merged"] == ["T7"] and r["final_suite"] == "pass",
               "the re-run recognises T4 as merged, continues with T7 and passes (got %s)" % r["summary"])
        expect("Conflict resolution notes" in open(os.path.join(rd, "integration.md")).read(),
               "the agent's resolution notes are kept in integration.md")

        print("a merge that breaks the suite is reverted")
        repo, rd, wt, base = build(os.path.join(tmp, "c"), [
            {"id": "T5", "files": {"BROKEN": "x\n"}},
            {"id": "T6", "deps": ["T5"], "files": {"app/f.py": "f=1\n"}},
            {"id": "T7", "files": {"app/g.py": "g=1\n"}}])
        r = run(rd)
        expect(r["suite_failed"] == ["T5"] and r["merged"] == ["T7"] and r["skipped"] == ["T6"],
               "T5 reverted, independent T7 merged, dependent T6 skipped (got %s)" % r["summary"])
        expect(not os.path.exists(os.path.join(wt, "BROKEN")) and r["status"] == "needs-human",
               "the broken file is gone from the worktree and the result asks for a human")
        expect(all("T5" not in s for s in merges(wt)), "no T5 merge is left in the history")

        print("unverified tasks are not merged")
        repo, rd, wt, base = build(os.path.join(tmp, "d"), [
            {"id": "T1", "files": {"app/a.py": "a=1\n"}},
            {"id": "T8", "files": {"app/h.py": "h=1\n"}, "status": "blocked"}])
        r = run(rd)
        expect(r["merged"] == ["T1"] and r["not_verified"] == ["T8"] and not os.path.exists(os.path.join(wt, "app", "h.py")),
               "a blocked task is reported and left out (got %s)" % r["not_verified"])

        print("the suite command is checked before anything runs")
        repo, rd, wt, base = build(os.path.join(tmp, "e"), [{"id": "T1", "files": {"app/a.py": "a=1\n"}}],
                                   "Suite command: `make build && make test`")
        r = run(rd)
        expect(r["status"] == "failed" and "shell operators" in r["summary"] and sh(wt, "rev-parse", "HEAD") == base,
               "a command with shell operators is rejected and nothing is merged")
        repo, rd, wt, base = build(os.path.join(tmp, "f"), [{"id": "T1", "files": {"app/a.py": "a=1\n"}}], "")
        r = run(rd)
        expect(r["status"] == "failed" and "no suite command" in r["summary"],
               "no suite command and nothing detectable is an error, not a silent pass")

        print("suite detection")
        for marker, content, want in [("gradlew", "x", "./gradlew test"), ("pom.xml", "x", "mvn -q test"),
                                      ("go.mod", "x", "go test ./..."), ("Cargo.toml", "x", "cargo test"),
                                      ("package.json", '{"scripts": {"test": "jest"}}', "npm test"),
                                      ("package.json", '{"scripts": {}}', None)]:
            d = os.path.join(tmp, "det-" + marker + str(want))
            put(os.path.join(d, marker), content)
            got = integrate.detect_suite(d)
            expect(got == want, "%-13s -> %s" % (marker, got))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
