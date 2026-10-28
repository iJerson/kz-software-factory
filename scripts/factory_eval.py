#!/usr/bin/env python3
"""Post-run eval for a software-factory run.

Checks whether each agent did what it was asked, and scores the run.

    python3 factory_eval.py <run-dir>                    scripted checks only
    python3 factory_eval.py <run-dir> --judge FILE       blend in the judge agent's rubric scores
    python3 factory_eval.py rubric                       print the judge rubric (markdown)

Writes eval.json and EVAL.md into the run directory and prints a summary.
The repo root is assumed to be three levels above the run directory
(<repo>/.factory/runs/<run-id>); override with --repo.

Scoring (starting weights; tune them after a few real runs):
  * every check has a severity: critical = 3, major = 2, minor = 1
  * agent score  = weighted pass rate over its applicable checks (n/a is ignored)
  * with a judge: agent score = 60% scripted + 40% judge (rubric items scored 0/1/2)
  * overall      = mean of agent scores, weighted by how much each agent was checked
  * verdict      = FAIL if any critical failure or overall < 70
                   WARN if any major failure or overall < 90
                   PASS otherwise
"""
import argparse
import collections
import fnmatch
import json
import os
import re
import subprocess
import sys

WEIGHT = {"critical": 3, "major": 2, "minor": 1}
DET_SHARE = 0.6
AGENT_ORDER = ["dispatcher", "spec", "planner", "builder", "verifier",
               "reviewer", "integrator", "reporter"]

MIN_TASK_LINES = 20  # a task that changed fewer lines than this was probably not worth its own agents
SHELL_OPERATORS = re.compile(r"[;&|<>`$]")
VALID_STATUS = {"ok", "needs-human", "failed"}
FINDING_SEVERITIES = {"blocker", "should-fix", "nit"}
FINDING_CATEGORIES = {"correctness", "security", "data-integrity", "maintainability",
                      "fit", "design", "ui", "accessibility"}
REVIEWERS = ("ui", "backend", "security")
# A reviewer's should-fix and nit findings must stay in its own domain (a blocker may not).
REVIEW_DOMAIN = {"ui": {"ui", "accessibility", "correctness", "maintainability", "fit", "design"},
                 "backend": {"correctness", "data-integrity", "maintainability", "fit", "design"},
                 "security": {"security", "data-integrity"}}
FAILURE_CLASSES = {"none", "spec-ambiguity", "test-failure", "env-error",
                   "wrong-approach", "scope-creep"}
NON_AGENT = {"gate", "cleanup", "run_end", "tokens_correction", "eval", "discarded", "route", "integrate"}
GATE_NAME = re.compile(r"^GATE ([123]|blocked)$")
GATE_STATUS = {"approved", "rejected", "waiting", "skipped"}
TEST_PATH = re.compile(r"(^|/)tests?(/|$)|(^|/)test_|_test\.|\.test\.|Tests?\.[A-Za-z]+$")

SPEC_SECTIONS = ["Goal", "Acceptance criteria", "Constraints", "Assumptions", "Out of scope", "OPEN"]
DESIGN_SECTIONS = ["Approach", "Interfaces", "Data model", "Key decisions", "Impact", "Risks"]
REPORT_SECTIONS = ["Outcome", "Acceptance criteria", "Tasks", "Review findings",
                   "Integration", "Cost", "Follow-ups"]

# (id, agent, severity, per_task, text). Scores: 0 = not done or contradicted by the
# evidence, 1 = partly, 2 = fully, null = not applicable.
RUBRIC = [
    ("S1", "spec", "major", False, "Every acceptance criterion is testable by a named command, test or observable behaviour. None is vague (\"works well\", \"handles errors\")."),
    ("S2", "spec", "major", False, "Ambiguities are surfaced as OPEN items or stated assumptions, not silently guessed. Nothing material in INPUT.md is dropped."),
    ("S3", "spec", "minor", False, "Stays in role: the spec says what and why, and does not choose an implementation approach."),
    ("D1", "planner", "major", False, "(design) Interfaces and contracts are exact enough (signatures, payloads, schemas) that parallel builders could code against them without talking to each other. Null if no design ran."),
    ("D2", "planner", "major", False, "(design) Key decisions are actually made, with rejected alternatives and reasons. No punting. Null if no design ran."),
    ("D3", "planner", "minor", False, "(design) Does not change or reinterpret SPEC.md. Null if no design ran."),
    ("P1", "planner", "critical", False, "Every definition of done is a runnable command or assertion that a correct implementation would pass. Flag checks that cannot pass, or that would pass regardless of the work."),
    ("P2", "planner", "major", False, "Tasks are small and single-purpose, correctly sequenced, with sensible model tiers and risk labels."),
    ("P3", "planner", "major", False, "The traceability section is accurate: each criterion maps to tasks that really satisfy it."),
    ("B1", "builder", "major", True, "Implemented what the task asked, within its scope, and its definition of done is met."),
    ("B2", "builder", "major", True, "result.md is honest: it reports doubts and what was run, and does not overclaim. No tests were weakened, skipped or deleted to get a pass."),
    ("V1", "verifier", "critical", True, "The evidence shows the checks were actually run (commands and output), not assumed from the builder's result.md."),
    ("V2", "verifier", "major", True, "The verdict is consistent with its evidence, the failure class is accurate, and fix_hint (if any) is specific."),
    ("R1", "reviewer", "major", True, "Findings are specific (file, line, concrete reason), non-generic, and not something a linter already enforces."),
    ("R2", "reviewer", "major", True, "Severity is calibrated: blockers are real defects, and no obvious defect in the diff was missed."),
    ("R3", "reviewer", "minor", True, "Design adherence was checked when a DESIGN.md exists. Null if no design ran."),
    ("R4", "reviewer", "major", True, "The right specialist reviewers ran for this diff (ui, backend, security): none that was needed was skipped, none that the diff did not need was spawned, and each stayed in its own domain. Null for an older run with a single reviewer."),
    ("I1", "integrator", "major", False, "Integration followed the script's rules (dependency order, the suite after each merge, breaking merges reverted), and if the integrator agent ran it only resolved the conflict it was given, without changing logic or tests."),
    ("REP1", "reporter", "critical", False, "Every acceptance criterion is reported with evidence. Nothing is marked met without evidence in the artifacts."),
    ("REP2", "reporter", "major", False, "Numbers (tasks, retries, tokens, findings) match the log and artifacts, and unknowns are stated as unknown."),
    ("DSP1", "dispatcher", "major", False, "The dispatcher held every gate, did not amend an approved artifact without asking the user, gave each agent only its listed inputs, and escalated blocked work instead of pushing through."),
]


# --------------------------------------------------------------------------- helpers

def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except OSError:
        return None, "missing"
    except ValueError as e:
        return None, "invalid json: %s" % e


def git(repo, *args):
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout.strip()


def branch_exists(repo, name):
    return git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + name)[0] == 0


def _name_re(name):
    return r"\s+".join(re.escape(w) for w in name.split())


def _heading_re(name):
    return r"(?im)^[\s#>*\-\d.)]*\**[ \t]*" + _name_re(name) + r"\b.*$"


def has_section(text, name):
    return re.search(_heading_re(name), text or "") is not None


def section_text(text, name, known=()):
    """Text under the heading `name`, up to the next markdown heading or known section."""
    text = text or ""
    m = re.search(_heading_re(name), text)
    if not m:
        return None
    rest = text[m.end():]
    ends = []
    n = re.search(r"(?m)^#{1,6}[ \t]", rest)
    if n:
        ends.append(n.start())
    for k in known:
        if k.lower() != name.lower():
            mk = re.search(_heading_re(k), rest)
            if mk:
                ends.append(mk.start())
    return rest[:min(ends)] if ends else rest


def criteria_list(spec):
    sec = section_text(spec, "Acceptance criteria", SPEC_SECTIONS)
    out = []
    for line in (sec or "").splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?\**(?:AC-?)?(\d+)[.):]\**\s*(.+)$", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def parse_table(text):
    header, rows, started = None, [], False
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if started and rows:
                break
            continue
        cells = [x.strip() for x in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", x) for x in cells if x):
            started = True
            continue
        if header is None:
            header = [x.lower().replace("`", "").replace("*", "").strip() for x in cells]
            continue
        rows.append(dict(zip(header, cells)))
    return header or [], rows


def _col(header, *aliases, exact=False):
    for h in header:
        if (h in aliases) if exact else any(a in h for a in aliases):
            return h
    return None


def parse_tasks(text):
    header, rows = parse_table(text)
    k = {"id": _col(header, "id", "task", exact=True), "title": _col(header, "title", "name"),
         "deps": _col(header, "depends"),
         "files": _col(header, "files"), "dod": _col(header, "definition", "dod"),
         "model": _col(header, "model"), "risk": _col(header, "risk"),
         "status": _col(header, "status")}
    ids = {r.get(k["id"], "").replace("`", "").strip() for r in rows} if k["id"] else set()
    tasks = []
    for r in rows:
        tid = r.get(k["id"], "").replace("`", "").strip() if k["id"] else ""
        if not tid:
            continue
        deps = [x for x in re.split(r"[,\s;/]+", r.get(k["deps"], "") if k["deps"] else "")
                if x in ids]
        raw_files = r.get(k["files"], "") if k["files"] else ""
        files = [x.replace("`", "").strip(" ()") for x in re.split(r"\s*(?:,|;|\n|\band\b)\s*", raw_files)]
        files = [x for x in files if x and x not in ("-", "—", "none", "n/a")]
        tasks.append({"id": tid, "title": (r.get(k["title"], "") if k["title"] else "").replace("`", "").strip(),
                      "deps": deps, "files": files,
                      "dod": r.get(k["dod"], "") if k["dod"] else "",
                      "model": (r.get(k["model"], "") if k["model"] else "").replace("`", "").strip().lower(),
                      "risk": (r.get(k["risk"], "") if k["risk"] else "").replace("`", "").strip().lower(),
                      "status": (r.get(k["status"], "") if k["status"] else "").replace("`", "").strip().lower()})
    return header, tasks, k


def reachable(tasks):
    deps = {t["id"]: t["deps"] for t in tasks}
    memo = {}

    def walk(tid, seen):
        if tid in memo:
            return memo[tid]
        out = set()
        for d in deps.get(tid, []):
            if d in seen:
                continue
            out.add(d)
            out |= walk(d, seen | {tid})
        memo[tid] = out
        return out

    return {t["id"]: walk(t["id"], set()) for t in tasks}


def has_cycle(tasks):
    deps = {t["id"]: t["deps"] for t in tasks}
    state = {}

    def visit(n):
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        if any(visit(d) for d in deps.get(n, []) if d in deps):
            return True
        state[n] = 2
        return False

    return any(visit(n) for n in deps)


class Results:
    def __init__(self):
        self.checks = []

    def add(self, cid, agent, severity, ok, detail="", task=None):
        self.checks.append({"id": cid, "agent": agent, "severity": severity,
                            "result": "na" if ok is None else ("pass" if ok else "fail"),
                            "task": task, "detail": detail})


# --------------------------------------------------------------------------- context

class Ctx:
    pass


def load_log(run_dir):
    events, malformed = [], 0
    path = os.path.join(run_dir, "agent_log.jsonl")
    if not os.path.exists(path):
        return events, malformed, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(e, dict):
                events.append(e)
    return events, malformed, True


def _key(e):
    return (e.get("agent"), e.get("task"), e.get("mode"), int(e.get("retries") or 0))


def merge_entries(events):
    entries = []
    for i, e in enumerate(events):
        if e.get("event") not in NON_AGENT and e.get("agent"):
            e = dict(e)
            e["_i"] = i
            entries.append(e)
    for c in events:
        if c.get("event") != "tokens_correction":
            continue
        for e in reversed(entries):
            if e.get("tokens") is None and _key(e) == _key(c):
                e["tokens"] = c.get("tokens")
                break
    return entries


def build_ctx(run_dir, repo):
    c = Ctx()
    c.run_dir = run_dir
    c.run_id = os.path.basename(run_dir)
    c.repo = repo
    state, c.state_err = load_json(os.path.join(run_dir, "STATE.json"))
    c.state = state if isinstance(state, dict) else {}
    c.events, c.malformed, c.log_exists = load_log(run_dir)
    c.entries = merge_entries(c.events)
    c.text = {n: read(os.path.join(run_dir, n)) for n in
              ["INPUT.md", "SPEC.md", "DESIGN.md", "TASKS.md", "integration.md", "REPORT.md"]}
    c.header, c.tasks, c.cols = parse_tasks(c.text["TASKS.md"])
    c.task_ids = [t["id"] for t in c.tasks] or sorted((c.state.get("tasks") or {}).keys())
    c.base_sha = c.state.get("base_sha")
    c.base_branch = c.state.get("base_branch") or "main"
    c.slug = c.state.get("slug") or re.sub(r"^\d{8}-\d{4}-", "", c.run_id)
    integ = c.state.get("integration") or {}
    c.integ_branch = integ.get("branch") or "feature/%s" % c.slug
    c.integ_worktree = integ.get("worktree") or ".factory/worktrees/%s" % c.slug
    dr = c.state.get("design_ran")
    c.design_ran = dr if isinstance(dr, bool) else c.text["DESIGN.md"] is not None
    budget = (c.state.get("budget") or {}).get("retries")
    if not isinstance(budget, int):
        m = re.search(r"(\d+)\s*retries", c.text["INPUT.md"] or "")
        budget = int(m.group(1)) if m else 2
    c.budget = budget
    try:
        c.review_skip_below = int(((c.state.get("review") or {}).get("skip_below")) or 0)
    except (TypeError, ValueError):
        c.review_skip_below = 0
    m = re.search(r"suite command\**\s*:\s*\**\s*`([^`]+)`", c.text["TASKS.md"] or "", re.I)
    c.suite_command = m.group(1).strip() if m else None
    ran = set()
    for t in c.task_ids:
        if os.path.isdir(os.path.join(run_dir, "tasks", t)):
            ran.add(t)
    ran |= {e["task"] for e in c.entries if e.get("task")}
    c.ran_tasks = sorted(ran)
    c.criteria = criteria_list(c.text["SPEC.md"])
    return c


def _idx(events, name):
    for i, e in enumerate(events):
        st = e.get("status") or e.get("decision")
        if (e.get("event") == "gate" and e.get("gate") == name
                and str(st or "").lower() in ("approved", "approve", "accepted")):
            return i
    return None


def _first(entries, agent):
    return next((e["_i"] for e in entries if e.get("agent") == agent), None)


# --------------------------------------------------------------------------- checks

def check_dispatcher(R, c):
    A = "dispatcher"
    R.add("dsp.log.parse", A, "major", c.log_exists and c.malformed == 0,
          "agent_log.jsonl missing" if not c.log_exists else "%d malformed log lines" % c.malformed)
    bad = [e for e in c.entries if e.get("status") not in VALID_STATUS]
    R.add("dsp.log.status", A, "major", not bad,
          "%d agent runs with a missing or invalid status" % len(bad))
    unknown = [e for e in c.entries if e.get("tokens") is None]
    R.add("dsp.log.tokens", A, "minor", not unknown,
          "%d agent runs have no token figure" % len(unknown))

    bad_gates = []
    for e in c.events:
        if e.get("event") == "gate" and (e.get("status") not in GATE_STATUS
                                         or not GATE_NAME.match(str(e.get("gate", "")))):
            bad_gates.append("%s/%s" % (e.get("gate"), e.get("status") or "decision=%s" % e.get("decision")))
    R.add("dsp.log.gates", A, "minor", not bad_gates,
          "nonstandard gate events (use GATE 1, 2, 3 or blocked, with status approved, rejected, "
          "waiting or skipped): %s" % ", ".join(bad_gates))
    dkeys = {_key(e) for e in c.events if e.get("event") == "discarded"}
    wasted, runs = 0, 0
    for e in c.entries:
        if _key(e) in dkeys or re.search(r"\b(not used|sidelined|discarded|abandoned)\b",
                                         str(e.get("note", "")), re.I):
            runs += 1
            wasted += e["tokens"] if isinstance(e.get("tokens"), (int, float)) else 0
    R.add("dsp.no_discarded_runs", A, "minor", runs == 0,
          "%d agent run(s) were discarded, wasting %s tokens (the user changed course after "
          "the agent had started)" % (runs, "{:,}".format(wasted)))

    p, b = _first(c.entries, "factory-planner"), _first(c.entries, "factory-builder")
    g1, g2 = _idx(c.events, "GATE 1"), _idx(c.events, "GATE 2")
    R.add("dsp.gate.spec", A, "major",
          None if p is None else (g1 is not None and g1 < p),
          "GATE 1 approval must be logged before the planner runs")
    R.add("dsp.gate.plan", A, "critical",
          None if b is None else (g2 is not None and g2 < b),
          "GATE 2 approval must be logged before any builder runs")
    gate3 = [e for e in c.events if e.get("event") == "gate" and e.get("gate") == "GATE 3"]
    R.add("dsp.gate.final", A, "major", bool(gate3), "no GATE 3 decision logged")

    keys_ok = all(k in c.state for k in ("run", "base_sha", "phase", "tasks"))
    R.add("dsp.state.valid", A, "major", c.state_err is None and keys_ok,
          c.state_err or "STATE.json is missing required keys")
    R.add("dsp.state.final", A, "major",
          None if not c.state else c.state.get("phase") in ("eval", "done", "aborted"),
          "phase is %r; expected eval, done or aborted" % c.state.get("phase"))

    retries = collections.defaultdict(int)
    for e in c.entries:
        if e.get("task"):
            retries[e["task"]] = max(retries[e["task"]], int(e.get("retries") or 0))
    over = {t: r for t, r in retries.items() if r > c.budget}
    R.add("dsp.retries.budget", A, "major", not over,
          "over the budget of %d retries: %s" % (c.budget, over))

    leftover = os.path.join(c.repo, ".factory", "worktrees", c.run_id)
    left_dir = os.path.isdir(leftover) and bool(os.listdir(leftover))
    rc, out = git(c.repo, "worktree", "list", "--porcelain")
    left_wt = rc == 0 and ("/.factory/worktrees/%s" % c.run_id) in out
    R.add("dsp.cleanup", A, "major", not (left_dir or left_wt),
          "task worktrees left behind under .factory/worktrees/%s" % c.run_id)
    R.add("dsp.naming", A, "minor",
          not re.search(r"(^|/)(integration|merge|final|combined)$", c.integ_branch),
          "integration branch %r has a generic name; name it after the feature" % c.integ_branch)

    decision = None
    if gate3:
        note = " ".join(str(e.get("note", "")) for e in gate3).lower()
        decision = next((d for d in ("apply", "leave", "discard") if d in note), None)
    head = git(c.repo, "rev-parse", c.base_branch)[1]
    # The integration worktree is kept for a PR after "leave" and removed after apply or
    # discard. During the eval itself (phase "eval") it is still expected to exist.
    if c.state.get("phase") == "done" and decision:
        have = os.path.isdir(os.path.join(c.repo, c.integ_worktree))
        want = decision == "leave"
        R.add("dsp.integration_worktree", A, "minor", have == want,
              "after %s the integration worktree %s should be %s, but it is %s"
              % (decision, c.integ_worktree, "kept" if want else "removed",
                 "present" if have else "missing"))
    else:
        R.add("dsp.integration_worktree", A, "minor", None,
              "only checked once the run is done and a decision is logged")
    if decision == "apply":
        if branch_exists(c.repo, c.integ_branch):
            ok = git(c.repo, "merge-base", "--is-ancestor", c.integ_branch, c.base_branch)[0] == 0
            R.add("dsp.apply", A, "critical", ok,
                  "apply was approved but %s is not in %s" % (c.integ_branch, c.base_branch))
        else:
            R.add("dsp.apply", A, "critical", None, "integration branch deleted; cannot verify")
    else:
        # No merge was requested (keep, discard, or no decision logged): the factory must
        # not have touched the base branch. This is the check for an unrequested merge.
        moved = bool(c.base_sha) and head != c.base_sha
        R.add("dsp.untouched", A, "critical", None if not c.base_sha else not moved,
              "the base branch %s moved off its starting commit but no merge was requested "
              "(decision: %s)" % (c.base_branch, decision or "none logged"))

    rc, out = git(c.repo, "branch", "-r", "--list", "*factory/%s*" % c.run_id, "*%s" % c.integ_branch)
    R.add("dsp.no_push", A, "major", rc == 0 and not out,
          "remote-tracking refs exist for this run's branches: %s" % out.replace("\n", " "))

    amend = []
    for i, e in enumerate(c.events):
        note = str(e.get("note", ""))
        if (re.search(r"\b(clarif|amend|rewr|reword|adjust)\w*", note, re.I)
                and re.search(r"\b(dod|definition of done|check|task)\b", note, re.I)):
            nxt = c.events[i + 1:i + 4]
            if not any(x.get("event") == "gate" for x in nxt):
                amend.append(e.get("task") or e.get("agent") or "?")
    R.add("dsp.no_silent_amend", A, "major", not amend,
          "heuristic: an approved task or check was amended with no gate logged after it (%s)"
          % ", ".join(map(str, amend)))


def check_spec(R, c):
    A, spec = "spec", c.text["SPEC.md"]
    R.add("spec.exists", A, "major", spec is not None, "SPEC.md missing")
    if spec is None:
        return
    missing = [s for s in SPEC_SECTIONS if not has_section(spec, s)]
    R.add("spec.sections", A, "major", not missing, "missing sections: %s" % ", ".join(missing))
    R.add("spec.criteria.numbered", A, "major", len(c.criteria) >= 1,
          "no numbered acceptance criteria found")
    vague = [n for n, t in c.criteria
             if re.search(r"\b(works? well|properly|correctly|appropriate(ly)?|user[- ]friendly|"
                          r"good|fast|robust|reasonable|as expected|nice)\b", t, re.I)
             and not re.search(r"[`\d]", t)]
    R.add("spec.criteria.vague", A, "minor", not vague,
          "criteria with vague wording and nothing checkable: %s" % vague)


def check_design(R, c):
    A, d = "planner", c.text["DESIGN.md"]
    if not c.design_ran:
        R.add("design.ran", A, "minor", None, "design did not run for this run")
        stray = sorted(f for f in os.listdir(c.run_dir)
                       if re.match(r"design.*\.md$", f, re.I)) if os.path.isdir(c.run_dir) else []
        R.add("design.no_stray_file", A, "minor", not stray,
              "design was skipped but the run directory has %s; a skip must leave no file" % ", ".join(stray))
        return
    R.add("design.exists", A, "major", d is not None, "DESIGN.md missing")
    if d is None:
        return
    missing = [s for s in DESIGN_SECTIONS if not has_section(d, s)]
    R.add("design.sections", A, "major", not missing, "missing sections: %s" % ", ".join(missing))
    R.add("design.interface_ids", A, "minor", re.search(r"\bI\d+\b", d) is not None,
          "no interface ids (I1, I2, ...) found")
    R.add("design.alternatives", A, "minor", re.search(r"alternativ|rejected", d, re.I) is not None,
          "key decisions do not mention rejected alternatives")
    R.add("design.no_open", A, "minor", re.search(r"(?im)^\s*[-*]?\s*OPEN:", d) is None,
          "unresolved OPEN: items remain in DESIGN.md")


def check_plan(R, c):
    A, t = "planner", c.text["TASKS.md"]
    R.add("plan.exists", A, "major", t is not None, "TASKS.md missing")
    if t is None:
        return
    need = {"id": c.cols["id"], "depends_on": c.cols["deps"], "files": c.cols["files"],
            "definition of done": c.cols["dod"], "model tier": c.cols["model"],
            "risk": c.cols["risk"], "status": c.cols["status"]}
    miss = [k for k, v in need.items() if not v]
    R.add("plan.columns", A, "major", not miss and bool(c.tasks),
          "missing columns: %s" % ", ".join(miss) if miss else "no task rows parsed")
    empty = [x["id"] for x in c.tasks if not x["dod"].strip()]
    R.add("plan.dod.present", A, "critical", bool(c.tasks) and not empty,
          "tasks with no definition of done: %s" % empty)
    soft = [x["id"] for x in c.tasks if x["dod"].strip()
            and not re.search(r"`|\bpass(es)?\b|\breturns?\b|\bexit\b|\b[1-5]\d\d\b|\bequals?\b", x["dod"], re.I)]
    R.add("plan.dod.mechanical", A, "minor", not soft,
          "definitions of done with no command or assertion: %s" % soft)
    R.add("plan.risk.values", A, "minor",
          all(x["risk"] in ("normal", "high") for x in c.tasks) if c.tasks else None,
          "risk must be normal or high")
    R.add("plan.model.values", A, "minor",
          all(x["model"] in ("haiku", "sonnet", "opus") for x in c.tasks) if c.tasks else None,
          "model tier must be haiku, sonnet or opus")
    ids = {x["id"] for x in c.tasks}
    raw_deps = c.cols["deps"]
    R.add("plan.depends.valid", A, "major", bool(c.tasks) and not has_cycle(c.tasks),
          "dependency cycle among tasks" if c.tasks else "no tasks")
    reach = reachable(c.tasks)
    clash = []
    for i, a in enumerate(c.tasks):
        for b in c.tasks[i + 1:]:
            if b["id"] in reach.get(a["id"], set()) or a["id"] in reach.get(b["id"], set()):
                continue
            shared = set(a["files"]) & set(b["files"])
            if shared:
                clash.append("%s/%s share %s" % (a["id"], b["id"], ", ".join(sorted(shared))))
    R.add("plan.parallel.overlap", A, "major", not clash,
          "tasks that can run in parallel touch the same files: %s" % "; ".join(clash))
    new_flow = "review" in c.state  # older runs predate the suite command line
    R.add("plan.suite_command", A, "major",
          None if not new_flow else (c.suite_command is not None and not SHELL_OPERATORS.search(c.suite_command)),
          "TASKS.md has no 'Suite command: `...`' line" if c.suite_command is None else
          "suite command %r contains shell operators; use one plain command" % c.suite_command)
    merge = []
    for i, a in enumerate(c.tasks):
        for b in c.tasks[i + 1:]:
            related = b["id"] in reach.get(a["id"], set()) or a["id"] in reach.get(b["id"], set())
            shared = set(a["files"]) & set(b["files"])
            if related and shared:
                merge.append("%s and %s (share %s)" % (a["id"], b["id"], ", ".join(sorted(shared))))
    R.add("plan.merge_candidates", A, "minor", not merge,
          "tasks sequenced only because they edit the same files; merge them: %s" % "; ".join(merge))
    trace = section_text(t, "Traceability", ["Traceability", "Uncovered"])
    if c.criteria:
        missing = []
        for n, _ in c.criteria:
            if not re.search(r"(?<![0-9A-Za-z])(?:AC-?)?%d(?![0-9])" % n, trace or ""):
                missing.append(n)
        R.add("plan.traceability", A, "major", trace is not None and not missing,
              "traceability section missing" if trace is None
              else "criteria not mapped to any task: %s" % missing)
    else:
        R.add("plan.traceability", A, "major", None, "no criteria parsed from SPEC.md")
    if c.design_ran and c.text["DESIGN.md"]:
        R.add("plan.design_refs", A, "minor", re.search(r"\bI\d+\b", t) is not None,
              "no task refers to a design interface id")


def check_tasks(R, c):
    sha = c.base_sha
    byid = {x["id"]: x for x in c.tasks}
    for tid in c.ran_tasks:
        tdir = os.path.join(c.run_dir, "tasks", tid)
        branch = "factory/%s/%s" % (c.run_id, tid)
        # --- builder
        R.add("build.result", "builder", "major", os.path.exists(os.path.join(tdir, "result.md")),
              "result.md missing", tid)
        have = sha and branch_exists(c.repo, branch)
        if not have:
            for cid in ("build.commits", "build.scope", "build.no_test_deletion", "build.size"):
                R.add(cid, "builder", "major", None, "branch %s not available" % branch, tid)
        else:
            rc, out = git(c.repo, "rev-list", "--count", "%s..%s" % (sha, branch))
            R.add("build.commits", "builder", "major", rc == 0 and out.isdigit() and int(out) > 0,
                  "no commits on %s" % branch, tid)
            changed = git(c.repo, "diff", "--name-only", "%s..%s" % (sha, branch))[1].splitlines()
            declared = byid.get(tid, {}).get("files", [])
            out_of_scope = []
            for f in changed:
                if TEST_PATH.search(f):
                    continue
                if any(f == d or f.startswith(d.rstrip("/") + "/") or fnmatch.fnmatch(f, d)
                       for d in declared):
                    continue
                out_of_scope.append(f)
            R.add("build.scope", "builder", "minor", not out_of_scope or not declared,
                  "files outside the task's declared files: %s" % ", ".join(out_of_scope[:6]), tid)
            # Scan every commit, not just base..tip: a test the builder added and
            # then deleted never shows up in a tip-versus-base diff. A file that was
            # deleted and then restored (present at the tip) is not counted.
            deleted = git(c.repo, "log", "--diff-filter=D", "--name-only", "--format=",
                          "%s..%s" % (sha, branch))[1].splitlines()
            gone = sorted({f for f in deleted if f and TEST_PATH.search(f)
                           and git(c.repo, "cat-file", "-e", "%s:%s" % (branch, f))[0] != 0})
            R.add("build.no_test_deletion", "builder", "critical", not gone,
                  "test files deleted: %s" % ", ".join(gone), tid)
            try:
                import route_reviewers
                rng = "%s..%s" % (sha, branch)
                files = [f for f in git(c.repo, "diff", "--name-only", rng)[1].splitlines() if f]
                nontrivial = [f for f in files if route_reviewers.classify(f) != "trivial"]
                lines = route_reviewers.changed_lines(c.repo, rng, nontrivial)
                R.add("build.size", "builder", "minor", lines >= MIN_TASK_LINES,
                      "%d changed lines in code files; under %d is too small to justify its own builder, "
                      "verifier and reviewers" % (lines, MIN_TASK_LINES), tid)
            except (ImportError, RuntimeError):
                R.add("build.size", "builder", "minor", None, "size not available", tid)

        # --- verifier
        v, err = load_json(os.path.join(tdir, "verdict.json"))
        R.add("verify.artifact", "verifier", "major",
              isinstance(v, dict) and all(k in v for k in
                                          ("task", "result", "failure_class", "checks", "evidence", "fix_hint")),
              err or "verdict.json is missing required keys", tid)
        if isinstance(v, dict):
            checks = v.get("checks") if isinstance(v.get("checks"), dict) else {}
            res, fc = v.get("result"), v.get("failure_class")
            if res == "pass":
                # The verifier prompt asks for a stated reason on a skipped check, so
                # "skipped (no linter configured)" counts as skipped.
                clean = all(x == "pass" or str(x).startswith("skipped") for x in checks.values()) \
                        and fc in ("none", None, "")
                R.add("verify.consistent", "verifier", "critical", clean,
                      "verdict is pass but a check failed or the failure class is set", tid)
                R.add("verify.done_ran", "verifier", "critical", checks.get("done") == "pass",
                      "definition-of-done check was %r; a skipped check cannot pass" % checks.get("done"), tid)
            elif res == "fail":
                R.add("verify.consistent", "verifier", "critical", fc in FAILURE_CLASSES - {"none"},
                      "verdict is fail but the failure class is %r" % fc, tid)
                R.add("verify.done_ran", "verifier", "critical", None, "verdict is fail", tid)
            else:
                R.add("verify.consistent", "verifier", "critical", False, "result is %r" % res, tid)
            R.add("verify.evidence", "verifier", "major",
                  len(str(v.get("evidence", "")).strip()) >= 40,
                  "evidence is empty or too short to show checks were run", tid)
        check_reviews(R, c, tid, tdir, v, branch, byid)

    # (reviewer checks are made by check_reviews, below)


def _kind(agent):
    agent = str(agent or "")
    return agent[len("factory-reviewer-"):] if agent.startswith("factory-reviewer-") else None


def _review_findings_ok(R, rv, rerr, kind, label, path_name):
    ok_top = isinstance(rv, dict) and isinstance(rv.get("findings"), list) and "summary" in rv
    R.add("review.artifact", "reviewer", "major", ok_top,
          rerr or "%s is missing summary or findings" % path_name, label)
    if not ok_top:
        return
    bad = []
    for i, f in enumerate(rv["findings"]):
        if not (isinstance(f, dict)
                and f.get("severity") in FINDING_SEVERITIES
                and f.get("category") in FINDING_CATEGORIES
                and isinstance(f.get("file"), str) and f.get("file")
                and isinstance(f.get("line"), int) and not isinstance(f.get("line"), bool)
                and isinstance(f.get("issue"), str) and isinstance(f.get("suggestion"), str)):
            bad.append(i)
    if kind and rv.get("reviewer") != kind:
        bad.append("reviewer field is %r, expected %r" % (rv.get("reviewer"), kind))
    R.add("review.schema", "reviewer", "major", not bad,
          "findings with missing or invalid fields (file, line, severity, ...): %s" % bad, label)
    vague = [i for i, f in enumerate(rv["findings"])
             if isinstance(f, dict) and len(str(f.get("issue", "")).strip()) < 20]
    R.add("review.specific", "reviewer", "minor", not vague,
          "findings with a one-line or empty issue: %s" % vague, label)
    nits = sum(1 for f in rv["findings"] if isinstance(f, dict) and f.get("severity") == "nit")
    R.add("review.nit_cap", "reviewer", "minor", nits <= 5, "%d nits (max 5)" % nits, label)
    if kind:
        off = [i for i, f in enumerate(rv["findings"])
               if isinstance(f, dict) and f.get("severity") != "blocker"
               and f.get("category") not in REVIEW_DOMAIN[kind]]
        R.add("review.domain", "reviewer", "minor", not off,
              "%s reviewer wrote non-blocker findings outside its domain: %s" % (kind, off), label)


def check_reviews(R, c, tid, tdir, verdict, branch, byid):
    revs = [e for e in c.entries if e.get("task") == tid and str(e.get("agent", "")).startswith("factory-reviewer")]
    spawned = {_kind(e["agent"]) for e in revs if _kind(e["agent"])}
    routes = [e for e in c.events if e.get("event") == "route" and e.get("task") == tid]
    routed = {k for r in routes for k in (r.get("reviewers") or [])}
    legacy_file = os.path.join(tdir, "review.json")
    legacy = not spawned and not routes and (os.path.exists(legacy_file)
                                             or any(e["agent"] == "factory-reviewer" for e in revs))
    passed = isinstance(verdict, dict) and verdict.get("result") == "pass"

    if not passed:
        R.add("review.artifact", "reviewer", "major", None, "task did not pass verification", tid)
    elif legacy:
        rv, rerr = load_json(legacy_file)
        _review_findings_ok(R, rv, rerr, None, tid, "review.json")
    else:
        # --- which reviewers ran, against which the diff needed
        R.add("review.routing.logged", "dispatcher", "minor", bool(routes),
              "no route event was logged for this task before its reviewers ran", tid)
        R.add("review.routing.spawned", "dispatcher", "major", spawned == routed,
              "reviewers spawned %s but routed %s" % (sorted(spawned), sorted(routed)), tid)
        needed = None
        sha = c.base_sha
        if sha and branch_exists(c.repo, branch):
            try:
                import route_reviewers
                risk = byid.get(tid, {}).get("risk") or "normal"
                needed = set(route_reviewers.compute(c.repo, sha, branch,
                                                     "high" if risk == "high" else "normal",
                                                     skip_below=c.review_skip_below)["reviewers"])
            except (ImportError, RuntimeError):
                needed = None
        if needed is None:
            R.add("review.routing.needed", "dispatcher", "major", None, "branch %s not available" % branch, tid)
            R.add("review.routing.extra", "dispatcher", "minor", None, "branch %s not available" % branch, tid)
        else:
            R.add("review.routing.needed", "dispatcher", "major", needed <= spawned,
                  "the diff needed %s but %s never ran" % (sorted(needed), sorted(needed - spawned)), tid)
            R.add("review.routing.extra", "dispatcher", "minor", spawned <= needed,
                  "%s ran although the diff has nothing in their domain (wasted tokens)"
                  % sorted(spawned - needed), tid)
        # --- each reviewer's own artifact
        for kind in sorted(routed | spawned):
            rv, rerr = load_json(os.path.join(tdir, "review.%s.json" % kind))
            _review_findings_ok(R, rv, rerr, kind, "%s:%s" % (tid, kind), "review.%s.json" % kind)

    # --- review loop and model choice, from the log (works for one reviewer or several)
    blds = [e for e in c.entries if e.get("task") == tid and e.get("agent") == "factory-builder"]
    state_status = ((c.state.get("tasks") or {}).get(tid) or {}).get("status")
    unhandled = []
    for r in revs:
        if int(r.get("blockers") or 0) > 0:
            followed = any(b["_i"] > r["_i"] and int(b.get("retries") or 0) > int(r.get("retries") or 0)
                           for b in blds)
            if not followed and state_status not in ("blocked", "skipped"):
                unhandled.append(r["_i"])
    R.add("review.blockers_handled", "reviewer", "major", not unhandled,
          "a review with blockers was not followed by a builder retry", tid)
    strong = [r for r in revs if _kind(r["agent"]) in ("backend", "security") or r["agent"] == "factory-reviewer"]
    if byid.get(tid, {}).get("risk") == "high" and strong:
        R.add("review.model_high_risk", "reviewer", "minor",
              all(r.get("model") == "opus" for r in strong),
              "high-risk task: backend and security were reviewed on %s"
              % sorted({str(r.get("model")) for r in strong}), tid)


def check_integrator(R, c):
    A, t = "integrator", c.text["integration.md"]
    R.add("integ.artifact", A, "major", t is not None, "integration.md missing")
    if t is not None:
        tail = "\n".join(t.strip().splitlines()[-30:])
        R.add("integ.suite", A, "minor",
              re.search(r"(suite|test)", tail, re.I) is not None and re.search(r"pass|green|ok", tail, re.I) is not None,
              "no final suite result found near the end of integration.md")


def check_reporter(R, c):
    A, r = "reporter", c.text["REPORT.md"]
    R.add("report.exists", A, "major", r is not None, "REPORT.md missing")
    if r is None:
        return
    missing = [s for s in REPORT_SECTIONS if not has_section(r, s)]
    R.add("report.sections", A, "major", not missing, "missing sections: %s" % ", ".join(missing))
    if c.criteria:
        sec = section_text(r, "Acceptance criteria", REPORT_SECTIONS) or ""
        pipes = len([x for x in sec.splitlines() if x.strip().startswith("|")])
        rows = max(0, pipes - 2)
        items = len([x for x in sec.splitlines() if re.match(r"^\s*(?:[-*]|\d+[.)])\s+", x)])
        R.add("report.criteria", A, "major", max(rows, items) >= len(c.criteria),
              "report lists %d of %d acceptance criteria" % (max(rows, items), len(c.criteria)))
    else:
        R.add("report.criteria", A, "major", None, "no criteria parsed from SPEC.md")
    unknown = [e for e in c.entries if e.get("tokens") is None]
    R.add("report.cost.unknowns", A, "minor",
          None if not unknown else re.search(r"unknown|not recorded|undercount|no token figure", r, re.I) is not None,
          "%d agent runs have no token figure but the report does not say so" % len(unknown))
    R.add("report.cost.own", A, "minor",
          re.search(r"(own|reporter'?s)\s+(cost|tokens?)", r, re.I) is not None,
          "report does not say the reporter's own cost is excluded")


# --------------------------------------------------------------------------- scoring

def load_judge(path, c):
    data, err = load_json(path)
    if err:
        return [], ["judge file: %s" % err]
    meta = {r[0]: r for r in RUBRIC}
    items, warns = [], []
    raw = data.get("items", []) if isinstance(data, dict) else []
    for it in raw:
        rid = it.get("id") if isinstance(it, dict) else None
        if rid not in meta:
            warns.append("unknown rubric id %r ignored" % (rid,))
            continue
        sc = it.get("score")
        if sc not in (0, 1, 2, None) or isinstance(sc, bool):
            warns.append("invalid score %r for %s ignored" % (sc, rid))
            continue
        _, agent, sev, _, _ = meta[rid]
        items.append({"id": rid, "agent": agent, "severity": sev, "task": it.get("task"),
                      "score": sc, "frac": None if sc is None else sc / 2.0,
                      "evidence": str(it.get("evidence", ""))[:300],
                      "note": str(it.get("note", ""))[:300],
                      "fix": str(it.get("fix", ""))[:300]})
    seen = {(i["id"], i["task"]) for i in items}
    missing = []
    for rid, agent, sev, per_task, _ in RUBRIC:
        if per_task:
            missing += ["%s/%s" % (rid, t) for t in c.ran_tasks if (rid, t) not in seen]
        elif (rid, None) not in seen:
            missing.append(rid)
    if missing:
        warns.append("judge did not score: %s" % ", ".join(missing))
    return items, warns


# --------------------------------------------------------------------------- action items

AGENT_FILE = {"dispatcher": ".claude/skills/software-factory/SKILL.md"}
for _a in AGENT_ORDER[1:]:
    AGENT_FILE[_a] = ".claude/agents/factory-%s.md" % _a
AGENT_FILE["reviewer"] = ".claude/agents/factory-reviewer-{ui,backend,security}.md"
AGENT_FILE["integrator"] = ".claude/skills/software-factory/scripts/integrate.py"
AGENT_FILE["reporter"] = ".claude/skills/software-factory/scripts/make_report.py"

# check id -> (what to do about this run, what to change in the factory so it does not recur).
# Placeholders: {tasks} {detail} {run_id} {slug} {base} {base_sha} {branch}
ACTIONS = {
    "dsp.log.parse": ("Open agent_log.jsonl and repair or delete the malformed lines ({detail}); the eval skipped them.",
                      "Log section: append one complete JSON object per line, in a single write."),
    "dsp.log.status": ("Find the agent runs in agent_log.jsonl with a missing or invalid status and correct them.",
                       "Agent contract: every agent ends with the status line (ok, needs-human or failed), and the dispatcher logs exactly that."),
    "dsp.log.tokens": ("Take the session total from Claude Code's usage view and record it as tokens_session_total on the run_end line.",
                       "Log section: append a tokens_correction line when a figure arrives late, so no agent run is left without one."),
    "dsp.log.gates": (None,
                      "Log section: gate events use status approved, rejected, waiting or skipped, and the names GATE 1, GATE 2, GATE 3 or GATE blocked; log one event per decision."),
    "dsp.no_discarded_runs": (None,
                              "Phase 2: settle the design mode at Gate 1 (or put Design mode in the request). A skip decided after design starts wastes that agent's tokens."),
    "dsp.gate.spec": ("Check that you did approve the spec; if not, review SPEC.md before relying on the plan.",
                      "Phase 1: never spawn the planner until GATE 1 is approved and logged."),
    "dsp.gate.plan": ("Builders ran before a GATE 2 approval was logged. Review the diff on each task branch before merging anything.",
                      "Phases 2 and 3: never spawn a builder until GATE 2 is approved and logged."),
    "dsp.gate.final": ("Decide what to do with the result: keep it, discard it, or merge it yourself.",
                       "Gate 3: always log the final decision as a gate event."),
    "dsp.state.valid": ("Run /software-factory resume {run_id} to rebuild STATE.json from the log and artifacts.",
                        "State and resume: write STATE.json atomically before and after every spawn."),
    "dsp.state.final": ("Run /software-factory resume {run_id} to finish the run and set the final phase.",
                        "Finish: set phase to done (or aborted) when the run ends."),
    "dsp.retries.budget": ("These tasks went over the retry budget: {detail}. Check whether they should have been escalated to you instead.",
                           "Phase 4: when a task reaches the retry budget, mark it blocked and escalate."),
    "dsp.cleanup": ("Remove the leftover task worktrees: git worktree remove .factory/worktrees/{run_id}/<task> for each, then git worktree prune.",
                    "Phase 5b: remove every task worktree right after integration, and log it."),
    "dsp.naming": ("Rename the branch after the feature: git branch -m {branch} feature/{slug}.",
                   "Naming: never name the integration branch generically; use feature/<slug>."),
    "dsp.integration_worktree": ("Match it to your decision. If you chose discard or a merge, remove it: git worktree remove .factory/worktrees/{slug}. If you chose to keep the result for a PR, bring it back: git worktree add .factory/worktrees/{slug} feature/{slug}.",
                                 "Finish: remove the integration worktree after a merge or discard, and keep it after leave."),
    "dsp.apply": ("A merge was requested but the feature branch is not in {base}. To finish it yourself, from {base}: git merge --ff-only feature/{slug}.",
                  "Gate 3: after a requested merge, verify the feature branch is an ancestor of the base before reporting success."),
    "dsp.untouched": ("The base branch moved without a merge request. Inspect git log {base_sha}..{base}. If the merge was not wanted and nothing else was committed since, move the branch back with git reset --hard {base_sha}.",
                      "Gate 3: never merge into the base branch unless the user asked for it in their own words."),
    "dsp.no_push": ("Delete the remote branches only if you did not push them yourself: git push origin --delete <branch>.",
                    "Safety defaults: never push to a remote."),
    "dsp.no_silent_amend": ("Check that the amended task or check still matches what you approved at GATE 2 ({detail}).",
                            "Ask the user before amending an approved task, and log a gate event for the answer."),
    "spec.exists": ("Re-run the spec phase, or write SPEC.md by hand.",
                    "factory-spec: always write SPEC.md; if the write is denied, return the text for the dispatcher to write."),
    "spec.sections": ("Add the missing sections to SPEC.md ({detail}).",
                      "factory-spec: write all six sections (Goal, Acceptance criteria, Constraints, Assumptions, Out of scope, OPEN) every time."),
    "spec.criteria.numbered": ("Number each acceptance criterion in SPEC.md.",
                               "factory-spec: number the acceptance criteria 1, 2, 3 so they can be traced."),
    "spec.criteria.vague": ("Rewrite the vague criteria ({detail}) so each names a command, a test or an observable behaviour.",
                            "factory-spec: reject criteria like \"works well\"; each one must be checkable."),
    "design.ran": (None, None),
    "design.no_stray_file": ("Delete the stray design file ({detail}).",
                             "Phase 2: when design is skipped, create no design file of any kind."),
    "design.exists": ("Re-run the design step or write DESIGN.md.",
                      "factory-planner (design mode): always write DESIGN.md."),
    "design.sections": ("Add the missing design sections ({detail}).",
                        "factory-planner (design mode): write every required section."),
    "design.interface_ids": ("Give each interface an id (I1, I2, ...) in DESIGN.md.",
                             "factory-planner (design mode): id every interface so tasks can refer to it."),
    "design.alternatives": ("Add the rejected alternatives and reasons to each key decision in DESIGN.md.",
                            "factory-planner (design mode): record rejected alternatives with reasons."),
    "design.no_open": ("Resolve the OPEN items in DESIGN.md, or answer them at the gate.",
                       "factory-planner (design mode): leave OPEN items only for what the user must decide."),
    "plan.exists": ("Re-run the plan step.", "factory-planner (plan mode): always write TASKS.md."),
    "plan.columns": ("Fix the TASKS.md table columns ({detail}).",
                     "factory-planner (plan mode): use the exact column set."),
    "plan.dod.present": ("Give every task a runnable definition of done in TASKS.md before building ({detail}).",
                         "factory-planner (plan mode): every task needs a definition of done."),
    "plan.dod.mechanical": ("Rewrite these definitions of done as commands or assertions: {detail}.",
                            "factory-planner (plan mode): a definition of done must be a command or assertion a verifier can run."),
    "plan.risk.values": ("Set risk to normal or high for every task in TASKS.md.",
                         "factory-planner (plan mode): risk is normal or high."),
    "plan.model.values": ("Set the model tier to haiku, sonnet or opus for every task in TASKS.md.",
                          "factory-planner (plan mode): model tier is haiku, sonnet or opus."),
    "plan.depends.valid": ("Remove the dependency cycle in TASKS.md.",
                           "factory-planner (plan mode): check depends_on for cycles before writing."),
    "plan.parallel.overlap": ("Add a depends_on between the tasks that share files, or split the files: {detail}.",
                              "factory-planner (plan mode): tasks that touch the same files must be sequenced."),
    "plan.traceability": ("Map every acceptance criterion to a task in the Traceability section ({detail}).",
                          "factory-planner (plan mode): every criterion must map to at least one task."),
    "plan.suite_command": ("Add the suite command to TASKS.md: the one command that runs the project's full test suite, with no shell operators ({detail}).",
                           "factory-planner (plan mode): always write a Suite command line; the integration script runs it after every merge."),
    "plan.merge_candidates": ("Consider merging these tasks into one before building: {detail}.",
                              "factory-planner (plan mode): merge tasks that edit the same files instead of sequencing them."),
    "plan.design_refs": ("Name the design interfaces each task implements in TASKS.md.",
                         "factory-planner (plan mode): refer to interface ids in tasks when a design exists."),
    "build.result": ("Re-run the builder for {tasks}, or write result.md from its commits.",
                     "factory-builder: always write result.md."),
    "build.commits": ("Task {tasks} has no commits. Re-run the builder for it.",
                      "factory-builder: commit your work to the task branch."),
    "build.scope": ("Review the files outside the task's scope before merging ({detail}).",
                    "factory-builder: stay inside the task's files; report needs-human instead of widening scope."),
    "build.size": ("Task {tasks} was tiny ({detail}). Next time fold it into a neighbouring task.",
                   "factory-planner (plan mode): avoid tasks under about 20 changed lines; each task costs a builder, a verifier and its reviewers."),
    "build.no_test_deletion": ("Restore the deleted tests on the branch for {tasks}: git checkout {base_sha} -- <file>, then commit. ({detail})",
                               "factory-builder: never delete or weaken tests to get a pass."),
    "verify.artifact": ("Re-run the verifier for {tasks}.", "factory-verifier: write verdict.json with every required key."),
    "verify.consistent": ("Re-run verification for {tasks}; the verdict contradicts its own checks.",
                          "factory-verifier: pass only if every check passes, and classify failures."),
    "verify.done_ran": ("Re-run verification for {tasks} with the definition-of-done check actually executed.",
                        "factory-verifier: a skipped definition-of-done check is a fail."),
    "verify.evidence": ("Re-run verification for {tasks} and record the commands and their output.",
                        "factory-verifier: put the commands run and their output in evidence."),
    "review.artifact": ("Re-run the reviewer for {tasks}.",
                        "The reviewer agents: write review.<kind>.json with a summary and findings."),
    "review.schema": ("Fix the findings in the review file for {tasks}: each needs severity, category, file, line, issue and suggestion, and the reviewer field must name the reviewer.",
                      "The reviewer agents: every finding needs a file and an integer line, and the reviewer field names the reviewer."),
    "review.specific": ("Make the review findings for {tasks} specific.",
                        "The reviewer agents: no generic advice; each finding says what is wrong and why."),
    "review.nit_cap": ("Trim the nits for {tasks} to the five that matter.", "The reviewer agents: at most five nits."),
    "review.domain": ("Move or drop the findings from {tasks} that are outside the reviewer's domain ({detail}).",
                      "The reviewer agents: write should-fix and nit findings only in your own domain; a blocker outside it is the one exception."),
    "review.blockers_handled": ("A blocker for {tasks} was never fixed. Re-run the builder and re-review.",
                                "Phase 4b: a blocker from any reviewer always sends the task back to a builder."),
    "review.model_high_risk": ("Re-review {tasks} on opus.",
                               "Phase 4b: for high-risk tasks the backend and security reviewers run on opus."),
    "review.routing.logged": (None,
                              "Phase 4b: always log a route event before spawning reviewers."),
    "review.routing.spawned": ("Check the review files for {tasks}: either a reviewer ran that was not routed, or a routed reviewer did not run.",
                               "Phase 4b: spawn exactly the reviewers the router returned, no more and no fewer."),
    "review.routing.needed": ("A reviewer the diff needed never looked at {tasks}. Spawn it now on the task branch: {detail}.",
                              "Phase 4b: run route_reviewers.py and spawn every reviewer it returns; do not override it with your own judgement."),
    "review.routing.extra": (None,
                             "Phase 4b: do not spawn a reviewer the router did not return. If the router is wrong for your codebase, tune the patterns in scripts/route_reviewers.py."),
    "integ.artifact": ("Run integrate.py again; it rewrites integration.md.",
                       "integrate.py: write integration.md on every run."),
    "integ.suite": ("Run integrate.py again so the final suite result is recorded.",
                    "integrate.py: end integration.md with the final suite result."),
    "report.exists": ("Run make_report.py to regenerate REPORT.md.",
                      "make_report.py: always write REPORT.md."),
    "report.sections": ("Run make_report.py to regenerate REPORT.md; it writes every section ({detail}).",
                        "make_report.py: write all seven sections every time."),
    "report.criteria": ("Run make_report.py to regenerate REPORT.md ({detail}).",
                        "make_report.py: one row per acceptance criterion."),
    "report.cost.unknowns": ("Run make_report.py to regenerate REPORT.md; it states unknown token figures.",
                             "make_report.py: state unknown token figures instead of hiding them."),
    "report.cost.own": ("Run make_report.py to regenerate REPORT.md.",
                        "make_report.py: say that no reporter cost was incurred."),
}


def build_actions(checks, judge_items, c):
    """Turn failed checks and low judge scores into prioritised, concrete action items."""
    base = {"run_id": c.run_id, "slug": c.slug, "base": c.base_branch,
            "base_sha": (c.base_sha or "<base_sha>")[:12], "branch": c.integ_branch}
    grouped = collections.OrderedDict()
    for ch in checks:
        if ch["result"] != "fail":
            continue
        g = grouped.setdefault(ch["id"], {"agent": ch["agent"], "severity": ch["severity"],
                                          "tasks": [], "detail": ch["detail"]})
        if ch["task"]:
            g["tasks"].append(ch["task"])
    items = []
    for cid, g in grouped.items():
        entry = ACTIONS.get(cid, (None, None))
        now, fix = entry[0], entry[1]
        fmt = dict(base, tasks=", ".join(g["tasks"]) or "the affected tasks", detail=g["detail"])
        items.append({"severity": g["severity"], "agent": g["agent"], "check": cid,
                      "tasks": g["tasks"], "evidence": g["detail"],
                      "now": now.format(**fmt) if now else (None if fix else g["detail"]),
                      "factory": fix.format(**fmt) if fix else None,
                      "where": AGENT_FILE[g["agent"]]})
    rubric = {r[0]: r for r in RUBRIC}
    for it in judge_items:
        if it["score"] is None or it["score"] >= 2:
            continue
        label = "judge:%s" % it["id"] + ("/%s" % it["task"] if it["task"] else "")
        text = rubric[it["id"]][4]
        items.append({"severity": it["severity"], "agent": it["agent"], "check": label,
                      "tasks": [it["task"]] if it["task"] else [],
                      "evidence": it["evidence"],
                      "now": it.get("fix") or it["note"] or "Review the evidence and correct it.",
                      "factory": ("Review the agent's prompt against this rubric item: %s" % text)
                      if it["score"] == 0 else None,
                      "where": AGENT_FILE[it["agent"]]})
    order = {"critical": 0, "major": 1, "minor": 2}
    items.sort(key=lambda i: (order[i["severity"]], AGENT_ORDER.index(i["agent"]), i["check"]))
    for n, i in enumerate(items, 1):
        i["n"] = n
    return items


def summarize(checks, judge_items):
    per = {a: {"det_w": 0.0, "det_pass": 0.0, "passed": 0, "failed": 0, "na": 0,
               "j_w": 0.0, "j_sum": 0.0, "j_n": 0} for a in AGENT_ORDER}
    critical = 0
    for ch in checks:
        p = per[ch["agent"]]
        if ch["result"] == "na":
            p["na"] += 1
            continue
        w = WEIGHT[ch["severity"]]
        p["det_w"] += w
        if ch["result"] == "pass":
            p["det_pass"] += w
            p["passed"] += 1
        else:
            p["failed"] += 1
            critical += ch["severity"] == "critical"
    for it in judge_items:
        if it["frac"] is None:
            continue
        p = per[it["agent"]]
        w = WEIGHT[it["severity"]]
        p["j_w"] += w
        p["j_sum"] += w * it["frac"]
        p["j_n"] += 1
        if it["score"] == 0 and it["severity"] == "critical":
            critical += 1
    agents, total_w, acc = {}, 0.0, 0.0
    for a, p in per.items():
        det = 100.0 * p["det_pass"] / p["det_w"] if p["det_w"] else None
        jdg = 100.0 * p["j_sum"] / p["j_w"] if p["j_w"] else None
        if det is None and jdg is None:
            continue
        score = det if jdg is None else jdg if det is None else DET_SHARE * det + (1 - DET_SHARE) * jdg
        weight = p["det_w"] + p["j_w"]
        total_w += weight
        acc += score * weight
        agents[a] = {"score": round(score, 1),
                     "scripted": None if det is None else round(det, 1),
                     "judge": None if jdg is None else round(jdg, 1),
                     "checks_passed": p["passed"], "checks_failed": p["failed"], "checks_na": p["na"]}
    overall = round(acc / total_w, 1) if total_w else 0.0
    major_fail = any(ch["result"] == "fail" and ch["severity"] == "major" for ch in checks)
    if critical or overall < 70:
        verdict = "FAIL"
    elif major_fail or overall < 90:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {"overall": overall, "verdict": verdict, "critical_failures": critical, "agents": agents}


def render_md(result):
    L = ["# Eval: %s" % result["run"], "",
         "**Verdict: %s**, overall score %.1f/100, %d critical failure(s)"
         % (result["verdict"], result["overall"], result["critical_failures"]), "",
         "| Agent | Score | Scripted | Judge | Passed | Failed | N/A |", "|---|---|---|---|---|---|---|"]
    for a in AGENT_ORDER:
        s = result["agents"].get(a)
        if s:
            L.append("| %s | %.1f | %s | %s | %d | %d | %d |" % (
                a, s["score"], "-" if s["scripted"] is None else "%.1f" % s["scripted"],
                "-" if s["judge"] is None else "%.1f" % s["judge"],
                s["checks_passed"], s["checks_failed"], s["checks_na"]))
    order = {"critical": 0, "major": 1, "minor": 2}
    failed = sorted([c for c in result["checks"] if c["result"] == "fail"],
                    key=lambda c: (order[c["severity"]], c["agent"], c["id"]))
    L += ["", "## Action items", ""]
    acts = result.get("actions") or []
    if not acts:
        L += ["None. Nothing failed and no judge item scored below 2."]
    for a in acts:
        where = "`%s`" % a["where"]
        L.append("%d. **[%s] %s: %s**%s" % (a["n"], a["severity"], a["agent"], a["check"],
                                           " (%s)" % ", ".join(a["tasks"]) if a["tasks"] else ""))
        if a["now"]:
            L.append("   - Do now: %s" % a["now"])
        if a["factory"]:
            L.append("   - Change the factory (%s): %s" % (where, a["factory"]))
        if a["evidence"] and a["evidence"] not in (a["now"] or ""):
            L.append("   - Why: %s" % a["evidence"])
    L += ["", "## Failed checks", ""]
    L += ["- [%s] %s %s%s: %s" % (c["severity"], c["agent"], c["id"],
                                   " (%s)" % c["task"] if c["task"] else "", c["detail"])
          for c in failed] or ["None."]
    low = [i for i in result.get("judge_items", []) if i["score"] is not None and i["score"] < 2]
    if result.get("judge_items"):
        L += ["", "## Judge findings (scored below 2)", ""]
        L += ["- [%s] %s %s%s scored %d/2: %s" % (
            i["severity"], i["agent"], i["id"], " (%s)" % i["task"] if i["task"] else "",
            i["score"], i["note"] or i["evidence"]) for i in low] or ["None."]
    if result.get("warnings"):
        L += ["", "## Warnings", ""] + ["- %s" % w for w in result["warnings"]]
    L += ["", "_Weights: critical 3, major 2, minor 1. With a judge, agent score = 60% scripted + 40% judge._"]
    return "\n".join(L) + "\n"


def evaluate(run_dir, repo=None, judge_path=None):
    run_dir = os.path.abspath(run_dir)
    repo = os.path.abspath(repo) if repo else os.path.abspath(os.path.join(run_dir, "..", "..", ".."))
    c = build_ctx(run_dir, repo)
    R = Results()
    check_dispatcher(R, c)
    check_spec(R, c)
    check_design(R, c)
    check_plan(R, c)
    check_tasks(R, c)
    check_integrator(R, c)
    check_reporter(R, c)
    items, warns = ([], [])
    if judge_path:
        items, warns = load_judge(judge_path, c)
    summary = summarize(R.checks, items)
    summary.update({"run": c.run_id, "checks": R.checks, "judge_items": items, "warnings": warns,
                    "actions": build_actions(R.checks, items, c),
                    "judge_used": bool(judge_path)})
    return summary


def rubric_markdown():
    L = ["Score each item 0, 1, 2 or null.", "",
         "- 2: fully done, with evidence you can point to", "- 1: partly done",
         "- 0: not done, or contradicted by the evidence", "- null: not applicable", ""]
    for rid, agent, sev, per_task, text in RUBRIC:
        L.append("- **%s** (%s, %s%s): %s" % (rid, agent, sev, ", once per task" if per_task else "", text))
    return "\n".join(L) + "\n"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "rubric":
        sys.stdout.write(rubric_markdown())
        return
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir")
    p.add_argument("--repo")
    p.add_argument("--judge")
    p.add_argument("--strict", action="store_true", help="exit 1 when the verdict is FAIL")
    a = p.parse_args()
    result = evaluate(a.run_dir, a.repo, a.judge)
    with open(os.path.join(a.run_dir, "eval.json"), "w") as f:
        json.dump(result, f, indent=2)
    md = render_md(result)
    with open(os.path.join(a.run_dir, "EVAL.md"), "w") as f:
        f.write(md)
    sys.stdout.write(md)
    if a.strict and result["verdict"] == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
