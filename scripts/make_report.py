#!/usr/bin/env python3
"""Generate REPORT.md for a finished run, with no agent.

    python3 make_report.py <run-dir> [--repo PATH]

Everything in the report comes from the run's artifacts, so nothing is estimated and
nothing needs a model: criteria status and evidence (from the traceability section,
each task's state, and the final suite result), the task table, review findings,
integration, cost, and follow-ups.

A criterion is
  met       every task that covers it is verified, and the final suite passed
  partly    some of its tasks are verified, or they are but the suite did not run
  not met   no task covers it, none of its tasks is verified, or the final suite failed

Writes REPORT.md in the run directory and prints one JSON line:
  {"status": "ok", "artifact": "<run-dir>/REPORT.md", "summary": "..."}
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_eval as fe  # noqa: E402


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.fullmatch(r"\s*([\d,]+)\s*", v)
        return int(m.group(1).replace(",", "")) if m else None
    return None


def trace_map(tasks_text, criterion_nums, task_ids):
    """criterion number -> set of task ids, read from the Traceability section.

    Understands lines like "1 -> T1, T2", "AC2: T3", "| 3 | T3 |" (criterion first) and
    "T1: criteria 1, 2" (task first).
    """
    sec = fe.section_text(tasks_text, "Traceability", ["Traceability", "Uncovered"]) or ""
    mapping = {n: set() for n in criterion_nums}
    ids = set(task_ids)
    for line in sec.splitlines():
        toks = re.findall(r"[A-Za-z]+\d+|\d+", line)
        found = [t for t in toks if t in ids]
        if not found:
            continue
        for t in toks:
            m = re.fullmatch(r"(?:AC)?(\d+)", t)
            if m and t not in ids and int(m.group(1)) in mapping:
                mapping[int(m.group(1))].update(found)
    return mapping


def criterion_status(tids, status_of, suite):
    if not tids:
        return "not met", "no task covers this criterion"
    verified = [t for t in tids if status_of(t) == "verified"]
    if not verified:
        return "not met", "none of its tasks is verified"
    if len(verified) < len(tids):
        return "partly", "only %s of %s verified" % (", ".join(verified), ", ".join(tids))
    if suite == "fail":
        return "not met", "its tasks are verified but the final suite failed"
    if suite == "pass":
        return "met", "all its tasks are verified and the final suite passed"
    if suite is None:
        return "met", "all its tasks are verified; no integration result was recorded"
    return "partly", "its tasks are verified but the final suite did not run"


def load_reviews(tdir):
    out = []
    for path in sorted(glob.glob(os.path.join(tdir, "review*.json"))):
        name = os.path.basename(path)
        kind = "reviewer" if name == "review.json" else name[len("review."):-len(".json")]
        data, _ = fe.load_json(path)
        if isinstance(data, dict):
            out.append((kind, data))
    return out


def build_report(run_dir, repo=None):
    run_dir = os.path.abspath(run_dir)
    repo = os.path.abspath(repo) if repo else os.path.abspath(os.path.join(run_dir, "..", "..", ".."))
    c = fe.build_ctx(run_dir, repo)
    tstate = c.state.get("tasks") or {}

    def status_of(tid):
        st = (tstate.get(tid) or {}).get("status")
        if st:
            return st
        t = next((x for x in c.tasks if x["id"] == tid), None)
        return (t or {}).get("status") or "unknown"

    ids = c.task_ids
    statuses = {t: status_of(t) for t in ids}
    retries = collections.defaultdict(int)
    reviewer_runs = collections.defaultdict(list)
    for e in c.entries:
        if e.get("task"):
            retries[e["task"]] = max(retries[e["task"]], int(e.get("retries") or 0))
            if str(e.get("agent", "")).startswith("factory-reviewer"):
                reviewer_runs[e["task"]].append(e)
    reviews = {t: load_reviews(os.path.join(run_dir, "tasks", t)) for t in ids}
    verdicts = {t: (fe.load_json(os.path.join(run_dir, "tasks", t, "verdict.json"))[0] or {}) for t in ids}
    integ = c.text["integration.md"]
    m = re.search(r"final suite:\s*(\w+)", integ or "", re.I)
    suite = m.group(1).lower() if m else None
    routes = {}
    for e in c.events:
        if e.get("event") == "route" and e.get("task"):
            routes[e["task"]] = e

    # ---- acceptance criteria
    nums = [n for n, _ in c.criteria]
    mapping = trace_map(c.text["TASKS.md"], nums, ids)
    crit_rows, counts = [], collections.Counter()
    for n, text in c.criteria:
        tids = sorted(mapping.get(n, []))
        result, why = criterion_status(tids, status_of, suite)
        counts[result] += 1
        ev = []
        for t in tids:
            kinds = [k for k, _ in reviews[t]]
            r = verdicts[t].get("result")
            bits = [status_of(t)]
            if r:
                bits.append("verifier %s" % r)
            if kinds:
                bits.append("reviewed by %s" % ", ".join(kinds))
            elif t in routes and not routes[t].get("reviewers"):
                bits.append("no review needed (small low-risk change)")
            ev.append("%s %s" % (t, "; ".join(bits)))
        evidence = "%s. %s" % ("; ".join(ev) if ev else "no tasks", why[:1].upper() + why[1:])
        crit_rows.append("| %d | %s | %s | %s |" % (n, text.strip().replace("|", "/")[:120], result,
                                                   evidence.replace("|", "/")))

    # ---- tasks
    def lines_changed(tid):
        branch = "factory/%s/%s" % (c.run_id, tid)
        if not (c.base_sha and fe.branch_exists(c.repo, branch)):
            return "-"
        try:
            import route_reviewers
            rng = "%s..%s" % (c.base_sha, branch)
            files = [f for f in fe.git(c.repo, "diff", "--name-only", rng)[1].splitlines() if f]
            return str(route_reviewers.changed_lines(
                c.repo, rng, [f for f in files if route_reviewers.classify(f) != "trivial"]))
        except Exception:
            return "-"

    title = {t["id"]: t["title"] for t in c.tasks}
    task_rows = []
    for t in ids:
        kinds = ", ".join(k for k, _ in reviews[t]) or "none"
        task_rows.append("| %s | %s | %s | %d | %s | %s |" % (
            t, (title.get(t) or "").replace("|", "/")[:60], statuses[t], retries[t], kinds, lines_changed(t)))

    # ---- review findings
    findings = []
    for t in ids:
        for kind, data in reviews[t]:
            for f in data.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                sev = f.get("severity", "?")
                tag = "BLOCKER (unresolved)" if sev == "blocker" else sev
                findings.append("- %s (%s, %s) %s:%s %s Suggestion: %s" % (
                    t, kind, tag, f.get("file", "?"), f.get("line", "?"),
                    str(f.get("issue", "")).strip()[:200], str(f.get("suggestion", "")).strip()[:160]))
    fixed = []
    for t in ids:
        n = sum(int(r.get("blockers") or 0) for r in reviewer_runs[t])
        if n and statuses[t] == "verified":
            fixed.append("%s: %d blocker(s) raised and fixed in %d retr%s" % (
                t, n, retries[t], "y" if retries[t] == 1 else "ies"))

    # ---- integration
    base = c.base_branch
    merged_into_base = None
    if fe.branch_exists(c.repo, c.integ_branch):
        merged_into_base = fe.git(c.repo, "merge-base", "--is-ancestor", c.integ_branch, base)[0] == 0
    suite_text = {"pass": "pass", "fail": "FAIL", "not": "not run (stopped at a conflict)",
                  None: "not recorded"}.get(suite, suite)

    # ---- cost
    by_model, unknown = collections.Counter(), 0
    for e in c.entries:
        n = _num(e.get("tokens"))
        if n is None:
            unknown += 1
        else:
            by_model[e.get("model", "?")] += n
    session = None
    for e in c.events:
        if e.get("event") == "run_end" and _num(e.get("tokens_session_total")) is not None:
            session = _num(e["tokens_session_total"])
    dkeys = {fe._key(e) for e in c.events if e.get("event") == "discarded"}
    wasted_runs = [e for e in c.entries if fe._key(e) in dkeys]
    wasted = sum(_num(e.get("tokens")) or 0 for e in wasted_runs)

    # ---- follow-ups
    follow = []
    for t in ids:
        if statuses[t] in ("blocked", "skipped", "integration-failed"):
            follow.append("%s is %s and is not in the result." % (t, statuses[t]))
    for n, _ in c.criteria:
        if not mapping.get(n):
            follow.append("Acceptance criterion %d is covered by no task." % n)
    shouldfix = sum(1 for f in findings if ", should-fix)" in f)
    if shouldfix:
        follow.append("%d should-fix finding(s) are listed above; they did not block the run." % shouldfix)
    if unknown:
        follow.append("%d agent run(s) have no token figure, so the cost total is an undercount." % unknown)
    if wasted_runs:
        follow.append("%d agent run(s) were discarded, wasting %s tokens." % (len(wasted_runs), "{:,}".format(wasted)))
    if merged_into_base is False:
        follow.append("The result is on `%s` and has not been merged into `%s`." % (c.integ_branch, base))

    total_c = len(c.criteria)
    st = collections.Counter(statuses.values())
    outcome = ("%d of %d acceptance criteria met" % (counts["met"], total_c)) if total_c else "No acceptance criteria were parsed"
    outcome += ("; %d of %d tasks verified" % (st["verified"], len(ids)))
    for k in ("blocked", "skipped", "integration-failed"):
        if st[k]:
            outcome += ", %d %s" % (st[k], k)
    outcome += "; %d retr%s; final suite %s." % (sum(retries.values()), "y" if sum(retries.values()) == 1 else "ies", suite_text)

    L = ["# Report: %s" % c.run_id, "", "## Outcome", "", outcome, "",
         "## Acceptance criteria", "", "| # | Criterion | Result | Evidence |", "|---|---|---|---|"]
    L += crit_rows or ["| - | none parsed from SPEC.md | - | - |"]
    L += ["", "## Tasks", "", "| Task | Title | Outcome | Retries | Reviewers | Lines changed |",
          "|---|---|---|---|---|---|"] + (task_rows or ["| - | none | - | - | - | - |"])
    L += ["", "## Review findings", ""]
    L += (findings + [""] if findings else ["No review findings."])
    if fixed:
        L += ["Blockers raised during the run:"] + ["- " + x for x in fixed]
    L += ["", "## Integration", "",
          "- Branch: `%s`; worktree: `%s`" % (c.integ_branch, c.integ_worktree),
          "- Final suite: %s" % suite_text,
          "- Merged into `%s`: %s" % (base, "yes" if merged_into_base else
                                      "no" if merged_into_base is False else "unknown (branch not found)"),
          "- To review: `git diff %s...%s`" % (base, c.integ_branch),
          "- To merge locally, from `%s`: `git merge --ff-only %s`, or push the branch and open a PR" % (base, c.integ_branch)]
    L += ["", "## Cost", "", "| Model | Tokens |", "|---|---|"]
    L += ["| %s | {:,} |".format(v) % k for k, v in sorted(by_model.items())] or ["| (none) | 0 |"]
    L += ["", "Agent runs: %d. %s" % (len(c.entries), (
        "%d have no token figure, which means unknown, not zero." % unknown) if unknown else
        "Every run reported a token figure.")]
    if session is not None:
        L.append("Session total from the usage view: {:,}.".format(session))
    L.append("These are the agents' own reported figures and can differ from the usage view. "
             "The reporter's own cost is zero: this report was generated by a script, not an agent.")
    L += ["", "## Follow-ups", ""] + (["- " + f for f in follow] or ["None."])
    summary = outcome
    return "\n".join(L) + "\n", summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--repo")
    a = ap.parse_args()
    text, summary = build_report(a.run_dir, a.repo)
    path = os.path.join(os.path.abspath(a.run_dir), "REPORT.md")
    with open(path, "w") as f:
        f.write(text)
    print(json.dumps({"status": "ok", "artifact": path, "summary": summary}))


if __name__ == "__main__":
    main()
