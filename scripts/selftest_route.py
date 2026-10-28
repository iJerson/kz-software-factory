#!/usr/bin/env python3
"""Self-test for route_reviewers.py.

    python3 selftest_route.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import route_reviewers as rr  # noqa: E402

CASES = [
    # (description, changed files, added text, risk, expected reviewers)
    ("backend source only", ["src/main/java/com/x/Foo.java"], "", "normal", ["backend"]),
    ("backend: Spring controller in a web package is not UI", ["src/main/java/com/x/web/UserController.java"], "", "normal", ["backend"]),
    ("markup and styles only", ["web/index.html", "web/style.css"], "", "normal", ["ui"]),
    ("component", ["src/components/Button.tsx"], "", "normal", ["ui"]),
    ("TypeScript outside a UI directory is backend", ["src/server/api.ts"], "", "normal", ["backend"]),
    ("TypeScript inside a UI directory is UI", ["src/pages/home.ts"], "", "normal", ["ui"]),
    ("Grails view", ["grails-app/views/user/show.gsp"], "", "normal", ["ui"]),
    ("Grails controller", ["grails-app/controllers/UserController.groovy"], "", "normal", ["backend"]),
    ("Flutter screen", ["lib/screens/home_screen.dart"], "", "normal", ["ui"]),
    ("Flutter service", ["lib/services/api.dart"], "", "normal", ["backend"]),
    ("UI and backend together", ["src/pages/home.ts", "src/server/api.ts"], "", "normal", ["ui", "backend"]),
    ("docs only", ["README.md", "docs/guide.md"], "", "normal", []),
    ("gitignore only", [".gitignore"], "", "normal", []),
    ("no files", [], "", "normal", []),
    ("high risk adds security", ["src/main/java/Foo.java"], "", "high", ["backend", "security"]),
    ("high risk with only docs adds nothing", ["README.md"], "", "high", []),
    ("auth path", ["src/auth/LoginService.java"], "", "normal", ["backend", "security"]),
    ("dependency manifest", ["package.json"], "", "normal", ["backend", "security"]),
    ("requirements.txt is a manifest, not a doc", ["requirements.txt"], "", "normal", ["backend", "security"]),
    ("CI workflow", [".github/workflows/ci.yml"], "", "normal", ["backend", "security"]),
    ("Dockerfile", ["Dockerfile"], "", "normal", ["backend", "security"]),
    (".env file", [".env.production"], "", "normal", ["backend", "security"]),
    ("tests only", ["tests/test_x.py"], "", "normal", ["backend"]),
    ("UI test file", ["src/Button.test.tsx"], "", "normal", ["ui"]),
    ("tokenizer is not a token path", ["src/lexer/tokenizer.py"], "", "normal", ["backend"]),
    ("SQL built by concatenation", ["src/Repo.java"], 'String q = "SELECT * FROM t WHERE id=" + id;', "normal", ["backend", "security"]),
    ("innerHTML in a component", ["src/components/Card.js"], "el.innerHTML = userText;", "normal", ["ui", "security"]),
    ("plain code has no security trigger", ["src/Foo.java"], "int total = a + b;", "normal", ["backend"]),
    ("docs mentioning a password trigger nothing", ["docs/setup.md"], "set the password in the console", "normal", []),
]


def sh(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)
    sh(repo, "add", path)


def cli(repo, base, branch, *extra):
    out = subprocess.run([sys.executable, os.path.join(HERE, "route_reviewers.py"), repo, base, branch]
                         + list(extra), capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


def main():
    failures = []

    def expect(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    print("routing rules")
    for desc, files, added, risk, want in CASES:
        got = rr.route(files, added, risk)["reviewers"]
        expect(got == want, "%-52s -> %s%s" % (desc, got, "" if got == want else "  (wanted %s)" % want))

    r = rr.route(["src/Foo.java"], "", "normal")
    expect("ui" in r["skipped"] and "backend" in r["reasons"],
           "a skipped reviewer says why, and a spawned one says why")

    print("skipping review for small low-risk changes")
    small = [
        ("small normal-risk backend change is skipped", dict(files=["src/Foo.java"], risk="normal", lines=12, skip_below=30), []),
        ("exactly at the threshold is reviewed", dict(files=["src/Foo.java"], risk="normal", lines=30, skip_below=30), ["backend"]),
        ("a large change is reviewed", dict(files=["src/Foo.java"], risk="normal", lines=200, skip_below=30), ["backend"]),
        ("high risk is never skipped", dict(files=["src/Foo.java"], risk="high", lines=3, skip_below=30), ["backend", "security"]),
        ("a small change with a security trigger is still reviewed", dict(files=["src/auth/Login.java"], risk="normal", lines=3, skip_below=30), ["backend", "security"]),
        ("a small change with a risky pattern is still reviewed", dict(files=["src/Repo.java"], added_text='"SELECT * FROM t WHERE id=" + id', risk="normal", lines=3, skip_below=30), ["backend", "security"]),
        ("skip_below 0 never skips", dict(files=["src/Foo.java"], risk="normal", lines=1, skip_below=0), ["backend"]),
        ("unknown size never skips", dict(files=["src/Foo.java"], risk="normal", lines=None, skip_below=30), ["backend"]),
        ("small UI change is skipped", dict(files=["web/index.html"], risk="normal", lines=5, skip_below=30), []),
    ]
    for desc, kw, want in small:
        got = rr.route(kw.pop("files"), kw.pop("added_text", ""), **kw)["reviewers"]
        expect(got == want, "%-58s -> %s%s" % (desc, got, "" if got == want else "  (wanted %s)" % want))
    expect("small low-risk" in rr.route(["src/Foo.java"], "", "normal", 5, 30)["skipped"]["all"],
           "a skipped review says why")

    print("git end to end, including --since for retries")
    tmp = tempfile.mkdtemp(prefix="route-selftest-")
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        sh(repo, "init", "-q", "-b", "main")
        sh(repo, "config", "user.email", "t@example.com")
        sh(repo, "config", "user.name", "tester")
        sh(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base = sh(repo, "rev-parse", "HEAD")
        sh(repo, "checkout", "-q", "-b", "task")
        write(repo, "src/Foo.java", "class Foo {}\n")
        sh(repo, "commit", "-q", "-m", "backend change")
        first = sh(repo, "rev-parse", "HEAD")
        out = cli(repo, base, "task")
        expect(out["reviewers"] == ["backend"], "backend-only branch routes to backend only (got %s)" % out["reviewers"])
        write(repo, "web/index.html", "<p>hi</p>\n")
        sh(repo, "commit", "-q", "-m", "ui fix after review")
        out = cli(repo, base, "task")
        expect(out["reviewers"] == ["ui", "backend"], "whole diff now needs ui and backend (got %s)" % out["reviewers"])
        out = cli(repo, base, "task", "--since", first)
        expect(out["reviewers"] == ["ui"],
               "--since the last review only needs ui, so the backend reviewer is not re-run (got %s)" % out["reviewers"])
        write(repo, "src/Db.java", 'String q = "SELECT * FROM t WHERE id=" + id;\n')
        sh(repo, "commit", "-q", "-m", "sql")
        out = cli(repo, base, "task")
        expect("security" in out["reviewers"] and "string" not in out["reasons"].get("security", "x") and
               "security patterns" in out["reasons"]["security"],
               "added SQL concatenation is read from the real diff and triggers security")
        out = cli(repo, base, "task", "--risk", "high")
        expect("security" in out["reviewers"], "--risk high triggers security")
        tip = sh(repo, "rev-parse", "HEAD")
        write(repo, "src/Tiny.java", "class Tiny {}\n")
        sh(repo, "commit", "-q", "-m", "tiny")
        out = cli(repo, base, "task", "--since", tip, "--skip-below", "30")
        expect(out["reviewers"] == [] and out["lines"] == 1,
               "a one-line retry diff is skipped with --skip-below 30 (got %s, %s lines)" % (out["reviewers"], out["lines"]))
        out = cli(repo, base, "task", "--since", tip)
        expect(out["reviewers"] == ["backend"], "the same one-line diff is reviewed without --skip-below")
        write(repo, "src/Big.java", "".join("int v%d = %d;\n" % (i, i) for i in range(60)))
        sh(repo, "commit", "-q", "-m", "big")
        out = cli(repo, base, "task", "--skip-below", "30")
        expect("backend" in out["reviewers"], "a large real diff is reviewed even with --skip-below 30")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
