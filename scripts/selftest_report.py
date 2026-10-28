#!/usr/bin/env python3
"""Self-test for make_report.py. It reuses the synthetic run from selftest_eval.py.

    python3 selftest_report.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_eval as fe  # noqa: E402
import make_report as mr  # noqa: E402
import selftest_eval as st  # noqa: E402


def fresh(tmp, name):
    repo, rd, base = st.build(os.path.join(tmp, name))
    os.remove(os.path.join(rd, "REPORT.md"))  # the generator must produce it
    return repo, rd, base


def report(rd):
    text, _ = mr.build_report(rd)
    with open(os.path.join(rd, "REPORT.md"), "w") as f:
        f.write(text)
    return text


def crit_row(text, n):
    return next(l for l in text.splitlines() if l.startswith("| %d |" % n))


def main():
    failures = []

    def expect(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    tmp = tempfile.mkdtemp(prefix="report-selftest-")
    try:
        print("healthy run")
        repo, rd, base = fresh(tmp, "ok")
        text = report(rd)
        expect(all(fe.has_section(text, s) for s in fe.REPORT_SECTIONS), "all seven sections are present")
        expect(all("| met |" in crit_row(text, n) for n in (1, 2, 3)), "all three criteria are met")
        expect("reviewed by backend, security" in crit_row(text, 2),
               "the evidence names the tasks, their verifier result and which reviewers ran")
        expect("3 of 3 acceptance criteria met" in text and "3 of 3 tasks verified" in text, "the outcome sentence is accurate")
        expect("reporter's own cost is zero" in text, "it says the reporter cost nothing")
        expect("Merged into `main`: yes" in text, "it reports whether the result is in the base branch")
        r = fe.evaluate(rd)
        bad = [c["id"] for c in r["checks"] if c["result"] == "fail"]
        expect(not bad, "the eval finds nothing wrong with the generated report (got %s)" % bad)

        print("a blocked task")
        repo, rd, base = fresh(tmp, "blocked")
        st.edit_json(rd, "STATE.json", lambda d: d["tasks"]["T3"].update(status="blocked"))
        text = report(rd)
        expect("| not met |" in crit_row(text, 3) and "| met |" in crit_row(text, 1),
               "only the criterion covered by the blocked task is not met")
        expect("T3 is blocked" in text and "2 of 3 criteria met" in text.replace("acceptance ", ""),
               "the follow-ups and outcome say so")

        print("an uncovered criterion")
        repo, rd, base = fresh(tmp, "uncovered")
        st.edit(rd, "TASKS.md", "- 3 -> T3\n", "")
        text = report(rd)
        expect("| not met |" in crit_row(text, 3) and "no task covers" in crit_row(text, 3).lower(),
               "a criterion with no task is not met, and says why")
        expect("covered by no task" in text, "and it is listed under follow-ups")

        print("partly met")
        repo, rd, base = fresh(tmp, "partly")
        st.edit_json(rd, "STATE.json", lambda d: d["tasks"]["T3"].update(status="blocked"))
        st.edit(rd, "TASKS.md", "- 1 -> T1\n", "- 1 -> T1, T3\n")
        text = report(rd)
        expect("| partly |" in crit_row(text, 1), "some tasks verified and some not is partly met")

        print("the final suite failed")
        repo, rd, base = fresh(tmp, "suitefail")
        st.edit(rd, "integration.md", "Final suite: pass", "Final suite: fail")
        text = report(rd)
        expect(all("| not met |" in crit_row(text, n) for n in (1, 2, 3)) and "final suite FAIL" in text,
               "verified tasks do not count as met when the integrated suite fails")

        print("unknown token figures")
        repo, rd, base = fresh(tmp, "tokens")
        p = os.path.join(rd, "agent_log.jsonl")
        lines = st.get(p).splitlines()
        d = json.loads(lines[0])
        d["tokens"] = None
        lines[0] = json.dumps(d)
        st.put(p, "\n".join(lines) + "\n")
        text = report(rd)
        expect("no token figure" in text and "unknown, not zero" in text, "unknowns are stated, not hidden")
        r = fe.evaluate(rd)
        expect("report.cost.unknowns" not in [c["id"] for c in r["checks"] if c["result"] == "fail"],
               "the eval accepts that wording")

        print("a task that skipped review")
        repo, rd, base = fresh(tmp, "skip")
        st.skip_small_reviews(rd)
        text = report(rd)
        expect("no review needed" in crit_row(text, 1), "the evidence says review was skipped because the change was small")

        print("reading the traceability section")
        ids = ["T1", "T2", "T3"]
        for desc, tasks_md, want in [
            ("arrow lines", "## Traceability\n- 1 -> T1, T2\n- 2 -> T3\n", {1: {"T1", "T2"}, 2: {"T3"}}),
            ("AC prefix", "## Traceability\nAC1: T1\nAC2: T2, T3\n", {1: {"T1"}, 2: {"T2", "T3"}}),
            ("table", "## Traceability\n| criterion | tasks |\n|---|---|\n| 1 | T1 |\n| 2 | T2 |\n", {1: {"T1"}, 2: {"T2"}}),
            ("task first", "## Traceability\nT1: criteria 1, 2\nT3: criterion 3\n", {1: {"T1"}, 2: {"T1"}, 3: {"T3"}}),
        ]:
            got = mr.trace_map(tasks_md, [1, 2, 3][:len(want) if len(want) == 3 else 2], ids)
            got = {k: v for k, v in got.items() if k in want}
            expect(got == want, "%-10s -> %s" % (desc, {k: sorted(v) for k, v in got.items()}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
