#!/usr/bin/env python3
"""Self-test for factory_eval.py.

Builds a synthetic, healthy factory run in a temporary git repo, checks that it
scores 100, then breaks it in specific ways and checks that the right check fails.

    python3 selftest_eval.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_eval as fe  # noqa: E402

RUN = "20261026-1000-demo"
SLUG = "demo"
INTEG = "feature/%s" % SLUG


def sh(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def get(path):
    with open(path) as f:
        return f.read()


SPEC = """# Spec

## Goal
Add two endpoints.

## Acceptance criteria
1. `GET /a` returns 200.
2. `GET /b` returns 200.
3. `python -m unittest` exits 0.

## Constraints
Standard library only.

## Assumptions
None.

## Out of scope
Authentication.

## OPEN
None.
"""

DESIGN = """# Design

## Approach
Two small modules.

## Interfaces and contracts
I1: `app.a.get()` returns a dict.
I2: `app.b.get()` returns a dict.

## Data model changes
None.

## Key decisions
Plain modules. Alternative rejected: a web framework, too heavy for two endpoints.

## Impact on existing code
None.

## Risks
Low.

## OPEN
None.
"""

TASKS = """# Tasks

| id | title | depends_on | files likely touched | definition of done | model tier | risk | status |
|---|---|---|---|---|---|---|---|
| T1 | Add a (I1) | - | `app/a.py` | `python -m unittest tests.test_a` passes | sonnet | normal | verified |
| T2 | Add b (I2) | - | `app/b.py`, `tests/test_b.py` | `python -m unittest tests.test_b` passes | sonnet | high | verified |
| T3 | Add suite | T1, T2 | `tests/test_all.py` | `python -m unittest` passes | haiku | normal | verified |

## Traceability
- 1 -> T1
- 2 -> T2
- 3 -> T3
"""

REPORT = """# Report

## Outcome
All three tasks verified and applied.

## Acceptance criteria
| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | GET /a returns 200 | met | T1 verdict |
| 2 | GET /b returns 200 | met | T2 verdict |
| 3 | suite exits 0 | met | integration.md |

## Tasks
T1, T2, T3 verified. T2 needed one retry.

## Review findings
T2: should-fix, app/b.py line 3.

## Integration
Branch merged in order. Suite passed.

## Cost
Tokens are known for every agent run. The reporter's own cost is not in the log.

## Follow-ups
None.
"""


def verdict(tid):
    return {"task": tid, "result": "pass", "failure_class": "none",
            "checks": {"build": "pass", "tests": "pass", "lint": "pass", "done": "pass",
                       "scope_and_tests": "pass"},
            "evidence": "ran python -m unittest for the task: OK, 1 test, 0 failures, build clean",
            "fix_hint": ""}


# Which specialist reviewers each task needs. T2 is high risk, so it also gets security.
REVIEW_PLAN = {"T1": ["backend"], "T2": ["backend", "security"], "T3": ["backend"]}


def review(tid, kind):
    findings = []
    if tid == "T2" and kind == "backend":
        findings.append({"severity": "should-fix", "category": "maintainability",
                         "file": "app/b.py", "line": 3,
                         "issue": "Function name hides that it performs IO on every call.",
                         "suggestion": "Rename it to fetch_b."})
    return {"task": tid, "reviewer": kind, "summary": "ok", "findings": findings}


def legacy_review(tid):
    d = review(tid, "backend")
    d.pop("reviewer")
    return d


def make_log(legacy=False):
    events, n = [], [0]

    def log(**k):
        n[0] += 1
        k.update(ts="2026-10-26T10:%02d:00Z" % n[0], run=RUN)
        events.append(k)

    def ret(agent, model, task=None, retries=0, **extra):
        log(task=task, agent=agent, model=model, event="agent_return", status="ok",
            retries=retries, tokens=1000, **extra)

    def reviews(task, kinds, retries=0, blockers=None, model_for=None):
        """Route, then spawn the routed reviewers (or one legacy reviewer)."""
        blockers = blockers or {}
        if legacy:
            ret("factory-reviewer", (model_for or {}).get("backend", "sonnet"), task,
                retries=retries, blockers=sum(blockers.values()))
            return
        log(event="route", task=task, retries=retries, reviewers=kinds,
            reasons={k: "x" for k in kinds}, skipped={})
        for k in kinds:
            ret("factory-reviewer-" + k, (model_for or {}).get(k, "sonnet"), task, retries=retries,
                blockers=blockers.get(k, 0))

    ret("factory-spec", "sonnet")
    log(event="gate", gate="GATE 1", status="approved", note="approve")
    ret("factory-planner", "opus", mode="design")
    ret("factory-planner", "opus", mode="plan")
    log(event="gate", gate="GATE 2", status="approved", note="approve")
    ret("factory-builder", "sonnet", "T1")
    ret("factory-verifier", "sonnet", "T1", verdict="pass")
    reviews("T1", REVIEW_PLAN["T1"])
    ret("factory-builder", "sonnet", "T2")
    ret("factory-verifier", "sonnet", "T2", verdict="pass")
    opus = {"backend": "opus", "security": "opus"}
    reviews("T2", REVIEW_PLAN["T2"], blockers={"backend": 1}, model_for=opus)
    ret("factory-builder", "sonnet", "T2", retries=1)
    ret("factory-verifier", "sonnet", "T2", retries=1, verdict="pass")
    reviews("T2", REVIEW_PLAN["T2"], retries=1, model_for=opus)
    ret("factory-builder", "haiku", "T3")
    ret("factory-verifier", "sonnet", "T3", verdict="pass")
    reviews("T3", REVIEW_PLAN["T3"])
    ret("factory-integrator", "sonnet")
    log(event="cleanup", removed=["T1", "T2", "T3"], kept=["integration worktree"], branches_kept=True)
    ret("factory-reporter", "haiku")
    log(event="gate", gate="GATE 3", status="approved", note="apply")
    log(event="run_end", tokens_known_total=19000, agents_without_tokens=0)
    return events


def build(tmp, legacy=False):
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.email", "t@example.com")
    sh(repo, "config", "user.name", "tester")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = sh(repo, "rev-parse", "HEAD")
    files = {"T1": {"app/a.py": "a = 1\n"},
             "T2": {"app/b.py": "b = 1\n", "tests/test_b.py": "def test():\n    pass\n"},
             "T3": {"tests/test_all.py": "def test():\n    pass\n"}}
    for t, fl in files.items():
        sh(repo, "checkout", "-q", "-b", "factory/%s/%s" % (RUN, t), base)
        for p, txt in fl.items():
            put(os.path.join(repo, p), txt)
            sh(repo, "add", p)
        sh(repo, "commit", "-q", "-m", "build " + t)
        sh(repo, "checkout", "-q", "main")
    sh(repo, "checkout", "-q", "-b", INTEG, base)
    for t in files:
        sh(repo, "merge", "-q", "--no-ff", "-m", "merge " + t, "factory/%s/%s" % (RUN, t))
    sh(repo, "checkout", "-q", "main")
    sh(repo, "merge", "-q", "--ff-only", INTEG)

    rd = os.path.join(repo, ".factory", "runs", RUN)
    put(os.path.join(rd, "INPUT.md"), "Budget: defaults, 2 retries per task, 3 parallel builders.\n")
    put(os.path.join(rd, "SPEC.md"), SPEC)
    put(os.path.join(rd, "DESIGN.md"), DESIGN)
    put(os.path.join(rd, "TASKS.md"), TASKS)
    put(os.path.join(rd, "integration.md"),
        "# Integration\nMerge order: T1, T2, T3.\nFull test suite passed after each merge. Final suite: pass.\n")
    put(os.path.join(rd, "REPORT.md"), REPORT)
    state = {"run": RUN, "slug": SLUG, "repo": repo, "base_branch": "main", "base_sha": base, "phase": "eval",
             "gate": {"name": "GATE 3", "status": "approved"}, "design_ran": True,
             "budget": {"retries": 2, "parallel": 3},
             "integration": {"branch": INTEG, "worktree": ".factory/worktrees/%s" % SLUG, "status": "merged"},
             "tasks": {"T1": {"status": "verified", "retries": 0},
                       "T2": {"status": "verified", "retries": 1},
                       "T3": {"status": "verified", "retries": 0}}}
    put(os.path.join(rd, "STATE.json"), json.dumps(state, indent=2))
    for t in files:
        put(os.path.join(rd, "tasks", t, "result.md"), "Implemented the task and ran its check.\n")
        put(os.path.join(rd, "tasks", t, "verdict.json"), json.dumps(verdict(t)))
        if legacy:
            put(os.path.join(rd, "tasks", t, "review.json"), json.dumps(legacy_review(t)))
        else:
            for kind in REVIEW_PLAN[t]:
                put(os.path.join(rd, "tasks", t, "review.%s.json" % kind), json.dumps(review(t, kind)))
    put(os.path.join(rd, "agent_log.jsonl"),
        "\n".join(json.dumps(e) for e in make_log(legacy)) + "\n")
    return repo, rd, base


# ----- mutations: each returns (description, expected failing check id)

def edit(rd, name, old, new):
    p = os.path.join(rd, name)
    s = get(p)
    assert old in s, "mutation target %r not found in %s" % (old, name)
    put(p, s.replace(old, new))


def edit_json(rd, name, fn):
    p = os.path.join(rd, name)
    d = json.loads(get(p))
    fn(d)
    put(p, json.dumps(d))


def filter_log(rd, keep):
    p = os.path.join(rd, "agent_log.jsonl")
    lines = [l for l in get(p).splitlines() if keep(json.loads(l))]
    put(p, "\n".join(lines) + "\n")


def m_no_gate2(repo, rd, base):
    filter_log(rd, lambda e: e.get("gate") != "GATE 2")
    return "dsp.gate.plan"


def m_pass_with_failed_check(repo, rd, base):
    edit_json(rd, "tasks/T3/verdict.json", lambda d: d["checks"].update(tests="fail"))
    return "verify.consistent"


def m_done_skipped(repo, rd, base):
    edit_json(rd, "tasks/T1/verdict.json", lambda d: d["checks"].update(done="skipped"))
    return "verify.done_ran"


def m_test_deleted(repo, rd, base):
    br = "factory/%s/T2" % RUN
    sh(repo, "checkout", "-q", br)
    sh(repo, "rm", "-q", "tests/test_b.py")
    sh(repo, "commit", "-q", "-m", "drop test")
    sh(repo, "checkout", "-q", "main")
    return "build.no_test_deletion"


def m_leftover_worktree(repo, rd, base):
    put(os.path.join(repo, ".factory", "worktrees", RUN, "T1", "x"), "x")
    return "dsp.cleanup"


def m_overlap(repo, rd, base):
    edit(rd, "TASKS.md", "| `app/a.py` |", "| `app/a.py`, `app/b.py` |")
    return "plan.parallel.overlap"


def m_report_section(repo, rd, base):
    edit(rd, "REPORT.md", "## Follow-ups\nNone.\n", "")
    return "report.sections"


def m_not_applied(repo, rd, base):
    sh(repo, "reset", "-q", "--hard", base)
    return "dsp.apply"


def m_blocker_unhandled(repo, rd, base):
    filter_log(rd, lambda e: not (e.get("agent") == "factory-builder" and e.get("task") == "T2"
                                  and e.get("retries") == 1))
    return "review.blockers_handled"


def m_review_no_line(repo, rd, base):
    def f(d):
        del d["findings"][0]["line"]
    edit_json(rd, "tasks/T2/review.backend.json", f)
    return "review.schema"


def m_no_dod(repo, rd, base):
    edit(rd, "TASKS.md", "`python -m unittest` passes", " ")
    return "plan.dod.present"


def m_silent_amend(repo, rd, base):
    p = os.path.join(rd, "agent_log.jsonl")
    s = get(p).splitlines()
    extra = json.dumps({"ts": "2026-10-26T10:05:30Z", "run": RUN, "task": "T3",
                        "event": "note", "note": "Dispatcher clarified the check for T3"})
    s.insert(8, extra)
    put(p, "\n".join(s) + "\n")
    return "dsp.no_silent_amend"


def m_no_trace(repo, rd, base):
    edit(rd, "TASKS.md", "- 3 -> T3\n", "")
    return "plan.traceability"


def m_unknown_tokens(repo, rd, base):
    p = os.path.join(rd, "agent_log.jsonl")
    lines = get(p).splitlines()
    d = json.loads(lines[0])
    d["tokens"] = None
    lines[0] = json.dumps(d)
    put(p, "\n".join(lines) + "\n")
    return "dsp.log.tokens"


def m_report_criteria(repo, rd, base):
    edit(rd, "REPORT.md", "| 3 | suite exits 0 | met | integration.md |\n", "")
    return "report.criteria"


def skip_design(rd, stray=None):
    os.remove(os.path.join(rd, "DESIGN.md"))
    edit_json(rd, "STATE.json", lambda d: d.update(design_ran=False))
    if stray:
        put(os.path.join(rd, stray), "Design was skipped.\n")


def m_stray_design(repo, rd, base):
    skip_design(rd, "DESIGN.skipped.md")
    return "design.no_stray_file"


def m_discarded(repo, rd, base):
    p = os.path.join(rd, "agent_log.jsonl")
    lines = get(p).splitlines()
    lines.insert(5, json.dumps({"ts": "2026-10-26T10:03:30Z", "run": RUN, "event": "discarded",
                                "agent": "factory-planner", "task": None, "mode": "design",
                                "retries": 0, "reason": "user skipped design"}))
    put(p, "\n".join(lines) + "\n")
    return "dsp.no_discarded_runs"


def m_gate_field(repo, rd, base):
    p = os.path.join(rd, "agent_log.jsonl")
    out = []
    for line in get(p).splitlines():
        e = json.loads(line)
        if e.get("gate") == "GATE 1":
            e["decision"] = e.pop("status")
        out.append(json.dumps(e))
    put(p, "\n".join(out) + "\n")
    return "dsp.log.gates"


def m_gate_name(repo, rd, base):
    p = os.path.join(rd, "agent_log.jsonl")
    lines = get(p).splitlines()
    lines.insert(4, json.dumps({"ts": "2026-10-26T10:02:30Z", "run": RUN, "event": "gate",
                                "gate": "GATE 1/2", "status": "skipped", "note": "skip design"}))
    put(p, "\n".join(lines) + "\n")
    return "dsp.log.gates"


def to_done(rd, decision, repo=None, base=None, keep_worktree=None):
    """Move a healthy run to phase 'done' with the given Gate 3 decision."""
    edit_json(rd, "STATE.json", lambda d: d.update(phase="done"))
    p = os.path.join(rd, "agent_log.jsonl")
    out = []
    for line in get(p).splitlines():
        e = json.loads(line)
        if e.get("gate") == "GATE 3":
            e["note"] = decision
        out.append(json.dumps(e))
    put(p, "\n".join(out) + "\n")
    if decision == "leave":
        sh(repo, "reset", "-q", "--hard", base)  # nothing was applied
    wt = os.path.join(repo, ".factory", "worktrees", SLUG)
    if keep_worktree:
        put(os.path.join(wt, "marker"), "x")


def m_unrequested_merge(repo, rd, base):
    # the user chose "leave", but the base branch was moved onto the feature branch anyway
    edit_json(rd, "STATE.json", lambda d: d.update(phase="done"))
    p = os.path.join(rd, "agent_log.jsonl")
    out = []
    for line in get(p).splitlines():
        e = json.loads(line)
        if e.get("gate") == "GATE 3":
            e["note"] = "leave"
        out.append(json.dumps(e))
    put(p, "\n".join(out) + "\n")
    return "dsp.untouched"


def m_merge_no_decision(repo, rd, base):
    filter_log(rd, lambda e: e.get("gate") != "GATE 3")
    return "dsp.untouched"


def m_ui_reviewer_missing(repo, rd, base):
    # T1 gains a UI file after routing, so a UI reviewer was needed but never ran
    sh(repo, "checkout", "-q", "factory/%s/T1" % RUN)
    put(os.path.join(repo, "web", "index.html"), "<p>hi</p>\n")
    sh(repo, "add", "web/index.html")
    sh(repo, "commit", "-q", "-m", "ui change")
    sh(repo, "checkout", "-q", "main")
    return "review.routing.needed"


def _rewrite_log(rd, fn):
    p = os.path.join(rd, "agent_log.jsonl")
    out = []
    for line in get(p).splitlines():
        out.extend(json.dumps(x) for x in fn(json.loads(line)))
    put(p, "\n".join(out) + "\n")


def m_unneeded_ui_reviewer(repo, rd, base):
    # T3 only touches backend files, yet a UI reviewer is routed and spawned for it
    def fn(e):
        if e.get("event") == "route" and e.get("task") == "T3":
            e["reviewers"] = ["backend", "ui"]
        out = [e]
        if e.get("agent") == "factory-reviewer-backend" and e.get("task") == "T3":
            out.append(dict(e, agent="factory-reviewer-ui"))
        return out
    _rewrite_log(rd, fn)
    put(os.path.join(rd, "tasks", "T3", "review.ui.json"), json.dumps(review("T3", "ui")))
    return "review.routing.extra"


def m_reviewer_not_routed(repo, rd, base):
    # a security reviewer ran on T1 although the route said backend only
    def fn(e):
        out = [e]
        if e.get("agent") == "factory-reviewer-backend" and e.get("task") == "T1":
            out.append(dict(e, agent="factory-reviewer-security"))
        return out
    _rewrite_log(rd, fn)
    put(os.path.join(rd, "tasks", "T1", "review.security.json"), json.dumps(review("T1", "security")))
    return "review.routing.spawned"


def m_review_file_missing(repo, rd, base):
    os.remove(os.path.join(rd, "tasks", "T2", "review.security.json"))
    return "review.artifact"


def m_review_domain(repo, rd, base):
    edit_json(rd, "tasks/T2/review.security.json", lambda d: d["findings"].append(
        {"severity": "should-fix", "category": "maintainability", "file": "app/b.py", "line": 3,
         "issue": "Function name hides that it performs IO on every call.", "suggestion": "Rename it."}))
    return "review.domain"


def m_route_not_logged(repo, rd, base):
    filter_log(rd, lambda e: not (e.get("event") == "route" and e.get("task") == "T3"))
    return "review.routing.logged"


def m_generic_name(repo, rd, base):
    edit_json(rd, "STATE.json", lambda d: d["integration"].update(branch="factory/%s/integration" % RUN))
    return "dsp.naming"


def m_worktree_left_after_apply(repo, rd, base):
    to_done(rd, "apply", repo, base, keep_worktree=True)
    return "dsp.integration_worktree"


def m_worktree_missing_after_leave(repo, rd, base):
    to_done(rd, "leave", repo, base, keep_worktree=False)
    return "dsp.integration_worktree"


MUTATIONS = [m_no_gate2, m_pass_with_failed_check, m_done_skipped, m_test_deleted,
             m_leftover_worktree, m_overlap, m_report_section, m_not_applied,
             m_blocker_unhandled, m_review_no_line, m_no_dod, m_silent_amend,
             m_no_trace, m_unknown_tokens, m_report_criteria, m_stray_design,
             m_discarded, m_gate_field, m_gate_name,
             m_generic_name, m_worktree_left_after_apply, m_worktree_missing_after_leave,
             m_unrequested_merge, m_merge_no_decision,
             m_ui_reviewer_missing, m_unneeded_ui_reviewer, m_reviewer_not_routed,
             m_review_file_missing, m_review_domain, m_route_not_logged]


def failed_ids(result):
    return sorted({c["id"] for c in result["checks"] if c["result"] == "fail"})


def judge_file(tmp, tasks, overrides=None):
    items = []
    for rid, _agent, _sev, per_task, _text in fe.RUBRIC:
        for t in (tasks if per_task else [None]):
            items.append({"id": rid, "task": t, "score": (overrides or {}).get(rid, 2),
                          "evidence": "e", "note": "n", "fix": "Add evidence for criterion 3."})
    p = os.path.join(tmp, "judge.json")
    put(p, json.dumps({"items": items}))
    return p


def main():
    failures = []

    def expect(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    tmp = tempfile.mkdtemp(prefix="eval-selftest-")
    try:
        print("healthy run")
        repo, rd, base = build(os.path.join(tmp, "clean"))
        r = fe.evaluate(rd)
        expect(not failed_ids(r), "no failed checks (got %s)" % failed_ids(r))
        expect(r["overall"] == 100.0 and r["verdict"] == "PASS",
               "score 100 and PASS (got %s, %s)" % (r["overall"], r["verdict"]))
        expect(r["actions"] == [] and "None." in fe.render_md(r),
               "a healthy run has no action items (got %d)" % len(r["actions"]))
        seen_ids = {c["id"] for c in r["checks"]}

        print("healthy run with design skipped")
        repo, rd, base = build(os.path.join(tmp, "skipped"))
        skip_design(rd)
        r = fe.evaluate(rd)
        expect(not failed_ids(r), "no failed checks when design is skipped and leaves no file (got %s)"
               % failed_ids(r))
        expect(any(c["id"] == "design.ran" and c["result"] == "na" for c in r["checks"]),
               "design checks are not applicable, not failed")

        print("older run with a single reviewer and no route events")
        repo, rd, base = build(os.path.join(tmp, "legacy"), legacy=True)
        r = fe.evaluate(rd)
        expect(not failed_ids(r), "a run with one review.json per task still scores clean (got %s)" % failed_ids(r))

        print("healthy runs at the end of a run")
        repo, rd, base = build(os.path.join(tmp, "applied"))
        to_done(rd, "apply", repo, base, keep_worktree=False)
        r = fe.evaluate(rd)
        expect(not failed_ids(r), "apply: integration worktree removed is correct (got %s)" % failed_ids(r))
        repo, rd, base = build(os.path.join(tmp, "left"))
        to_done(rd, "leave", repo, base, keep_worktree=True)
        r = fe.evaluate(rd)
        expect(not failed_ids(r), "leave: integration worktree kept for the PR is correct (got %s)" % failed_ids(r))

        print("mutations (each must fail the expected check and produce an action item)")
        no_action = []
        for i, m in enumerate(MUTATIONS):
            repo, rd, base = build(os.path.join(tmp, "m%d" % i))
            expected = m(repo, rd, base)
            res = fe.evaluate(rd)
            got = failed_ids(res)
            extra = [x for x in got if x != expected]
            expect(expected in got, "%-28s -> %-26s %s (verdict %s, score %.1f)" % (
                m.__name__, expected, "also failed: %s" % extra if extra else "no collateral",
                res["verdict"], res["overall"]))
            seen_ids |= {c["id"] for c in res["checks"]}
            act = next((a for a in res["actions"] if a["check"] == expected), None)
            if not (act and (act["now"] or act["factory"])):
                no_action.append(expected)
            if len(res["actions"]) != len(got):
                no_action.append("count mismatch for " + m.__name__)

        expect(not no_action, "every failed check produced an action item (problems: %s)" % no_action)
        unmapped = sorted(i for i in seen_ids if i not in fe.ACTIONS)
        expect(not unmapped, "every check id has an action mapping (unmapped: %s)" % unmapped)

        print("judge blending")
        repo, rd, base = build(os.path.join(tmp, "judge"))
        ok = fe.evaluate(rd, judge_path=judge_file(tmp, ["T1", "T2", "T3"]))
        expect(ok["overall"] == 100.0 and ok["verdict"] == "PASS" and not ok["warnings"],
               "all-2 judge keeps 100/PASS (got %s, %s, %s)" % (ok["overall"], ok["verdict"], ok["warnings"]))
        bad = fe.evaluate(rd, judge_path=judge_file(tmp, ["T1", "T2", "T3"], {"REP1": 0}))
        expect(bad["verdict"] == "FAIL" and bad["critical_failures"] == 1,
               "a 0 on a critical rubric item forces FAIL (got %s, %d critical)"
               % (bad["verdict"], bad["critical_failures"]))
        expect(bad["agents"]["reporter"]["judge"] is not None and bad["agents"]["reporter"]["judge"] < 100,
               "the reporter's judge score drops")
        jact = next((a for a in bad["actions"] if a["check"] == "judge:REP1"), None)
        expect(jact is not None and jact["now"] == "Add evidence for criterion 3." and jact["factory"],
               "a low judge score becomes an action item using the judge's fix")
        expect(bad["actions"][0]["severity"] == "critical" and "## Action items" in fe.render_md(bad),
               "critical action items come first and are rendered in EVAL.md")
        partial = fe.evaluate(rd, judge_path=judge_file(tmp, ["T1"]))
        expect(any("did not score" in w for w in partial["warnings"]),
               "a judge that skips items is flagged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
