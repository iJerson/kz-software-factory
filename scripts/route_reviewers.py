#!/usr/bin/env python3
"""Decide which specialist reviewers a task needs, from the files it changed.

    python3 route_reviewers.py <repo> <base_sha> <branch> [--risk normal|high] [--since SHA]
                               [--skip-below N]

Prints one JSON object:
    {"reviewers": ["ui", "backend", "security"],   # only those that are needed
     "reasons":   {"backend": "2 backend file(s): ..."},
     "skipped":   {"ui": "no UI files changed"},
     "files":     {"ui": [...], "backend": [...], "trivial": [...]},
     "range":     "<base>..<branch>"}

Rules (all deterministic, so the dispatcher spends no tokens on routing):
  ui        any UI file changed (markup, styles, components, templates, UI assets)
  backend   any non-UI file changed that is not documentation or a trivial dotfile
  security  the task is high risk, or a changed path looks security-sensitive
            (auth, secrets, crypto, CI or infrastructure files, dependency manifests),
            or the added lines match a security pattern (SQL built by string
            concatenation, innerHTML, eval, shell execution, deserialization, ...)
  none      only documentation or trivial files changed, so no review is needed

Small changes: with --skip-below N (the dispatcher passes 30 in review mode "auto"), a task
that is not high risk, has no security trigger, and changed fewer than N lines gets no
review at all. A binary file counts as 100 lines, so assets never slip through as "small".

With --since, only the files changed since that commit are considered. The dispatcher
uses it after a retry to re-run only the reviewers whose domain was touched.

These are heuristics. A false positive costs one extra review; a false negative skips a
review that was needed, so the security patterns lean towards spawning.
"""
import argparse
import json
import os
import re
import subprocess
import sys

UI_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sass", ".less",
          ".styl", ".gsp", ".jsp", ".ftl", ".hbs", ".mustache", ".ejs", ".pug", ".erb", ".xaml",
          ".storyboard", ".xib", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
          ".woff", ".woff2", ".ttf", ".otf"}
# Code extensions that are only UI when they live in a UI directory.
AMBIGUOUS_EXT = {".js", ".ts", ".mjs", ".cjs", ".dart", ".kt", ".java", ".swift", ".groovy"}
UI_DIRS = {"components", "component", "pages", "views", "screens", "widgets", "ui", "frontend",
           "front-end", "templates", "static", "public", "assets", "styles", "layouts", "themes",
           "stories", "webapp"}
UI_NAME = re.compile(r"_(screen|page|widget|view)\.dart$|\.stories\.[a-z]+$", re.I)

DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
TRIVIAL_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "license", "license.md",
                 "changelog", "changelog.md", ".dockerignore", ".npmignore"}
DOC_DIRS = {"docs", "doc"}

MANIFESTS = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pipfile",
             "pipfile.lock", "poetry.lock", "pyproject.toml", "pom.xml", "build.gradle",
             "build.gradle.kts", "settings.gradle", "gradle.lockfile", "go.mod", "go.sum",
             "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock", "composer.json",
             "composer.lock", "pubspec.yaml", "pubspec.lock"}
SEC_PATH = re.compile(
    r"(^|[/_.\-])(auth|authn|authz|login|logout|signin|signup|password|passwd|secret|secrets|"
    r"credential|credentials|token|tokens|session|sessions|crypto|security|permission|"
    r"permissions|acl|rbac|oauth|jwt|saml|sso|cors|csrf|firewall|iam|policy|policies|"
    r"keystore|truststore)([/_.\-]|$)", re.I)
SEC_EXT = {".pem", ".key", ".p12", ".jks", ".keystore", ".crt", ".cer", ".tf", ".tfvars"}
SEC_NAMES = ("dockerfile", "docker-compose", "jenkinsfile", ".gitlab-ci.yml", "serverless.yml",
             "bitbucket-pipelines.yml")
SEC_PREFIXES = (".github/workflows/", ".circleci/")
SEC_CONTENT = re.compile(
    r"\b(password|passwd|secret|api[_-]?key|private[_-]?key|jwt|bearer|oauth|credential|"
    r"authoriz\w*|authenticat\w*|encrypt\w*|decrypt\w*|hmac|csrf|cors|innerHTML|"
    r"dangerouslySetInnerHTML|deserializ\w*|ObjectInputStream|pickle|permitAll|PreAuthorize|"
    r"executeQuery|createStatement|ProcessBuilder|os\.system|subprocess|child_process)\b"
    r"|Runtime\.getRuntime|\beval\(|\bexec\(|[\"']\s*(SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*\+",
    re.I)


def is_manifest(name):
    return name in MANIFESTS or re.match(r"requirements[\w.\-]*\.txt$", name) is not None


def classify(path):
    """Returns 'ui', 'backend' or 'trivial' for one changed file."""
    low = path.lower()
    name = os.path.basename(low)
    ext = os.path.splitext(name)[1]
    parts = set(low.split("/")[:-1])
    if is_manifest(name):
        return "backend"
    if name in TRIVIAL_NAMES or (ext in DOC_EXT) or (parts & DOC_DIRS):
        return "trivial"
    if ext in UI_EXT or UI_NAME.search(low):
        return "ui"
    if ext in AMBIGUOUS_EXT and (parts & UI_DIRS):
        return "ui"
    return "backend"


def security_path_reasons(path):
    low = path.lower()
    name = os.path.basename(low)
    out = []
    if is_manifest(name):
        out.append("dependency manifest %s" % path)
    if SEC_PATH.search(low):
        out.append("security-sensitive path %s" % path)
    if os.path.splitext(name)[1] in SEC_EXT or name.startswith(".env"):
        out.append("secrets or infrastructure file %s" % path)
    if name.startswith(SEC_NAMES) or low.startswith(SEC_PREFIXES):
        out.append("CI or deployment file %s" % path)
    return out


def route(files, added_text="", risk="normal", lines=None, skip_below=0):
    groups = {"ui": [], "backend": [], "trivial": []}
    for f in files:
        groups[classify(f)].append(f)
    reviewed = groups["ui"] + groups["backend"]
    sec = []
    if reviewed:
        if risk == "high":
            sec.append("task risk is high")
        for f in reviewed:
            sec += security_path_reasons(f)
        hits = sorted({m.group(0).lower() for m in SEC_CONTENT.finditer(added_text or "")})
        if hits:
            sec.append("added code matches security patterns: %s" % ", ".join(hits[:5]))
    reasons, skipped, reviewers = {}, {}, []
    if groups["ui"]:
        reviewers.append("ui")
        reasons["ui"] = "%d UI file(s): %s" % (len(groups["ui"]), ", ".join(groups["ui"][:4]))
    else:
        skipped["ui"] = "no UI files changed"
    if groups["backend"]:
        reviewers.append("backend")
        reasons["backend"] = "%d backend file(s): %s" % (len(groups["backend"]), ", ".join(groups["backend"][:4]))
    else:
        skipped["backend"] = "no backend files changed"
    if sec:
        reviewers.append("security")
        reasons["security"] = "; ".join(sec[:4])
    else:
        skipped["security"] = "no security trigger" if reviewed else "nothing to review"
    if (reviewers and skip_below > 0 and lines is not None and lines < skip_below
            and risk != "high" and not sec):
        reviewers, reasons = [], {}
        skipped = {"all": "small low-risk change: %d changed lines, under %d" % (lines, skip_below)}
    if not reviewers and "all" not in skipped:
        skipped["all"] = "only documentation or trivial files changed" if files else "no files changed"
    return {"reviewers": reviewers, "reasons": reasons, "skipped": skipped, "files": groups,
            "lines": lines}


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args[:2]), p.stderr.strip()))
    return p.stdout


def changed_lines(repo, rng, paths):
    """Added plus deleted lines in `paths` over the range. A binary file counts as 100."""
    if not paths:
        return 0
    total = 0
    for row in _git(repo, "diff", "--numstat", rng, "--", *paths).splitlines():
        a, d = (row.split("\t") + ["0", "0"])[:2]
        total += 100 if a == "-" or d == "-" else int(a) + int(d)
    return total


def compute(repo, base_sha, branch, risk="normal", since=None, skip_below=0):
    """Route a task straight from git. Raises RuntimeError if git cannot answer."""
    rng = "%s..%s" % (since or base_sha, branch)
    files = [f for f in _git(repo, "diff", "--name-only", rng).splitlines() if f]
    reviewed = [f for f in files if classify(f) != "trivial"]
    added = ""
    if reviewed:
        diff = _git(repo, "diff", "-U0", rng, "--", *reviewed)
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    out = route(files, added, risk, changed_lines(repo, rng, reviewed), skip_below)
    out["range"] = rng
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("base_sha")
    ap.add_argument("branch")
    ap.add_argument("--risk", default="normal", choices=["normal", "high"])
    ap.add_argument("--since", help="only consider files changed since this commit")
    ap.add_argument("--skip-below", type=int, default=0,
                    help="skip review for a low-risk task that changed fewer than N lines (0 = never skip)")
    a = ap.parse_args()
    try:
        print(json.dumps(compute(a.repo, a.base_sha, a.branch, a.risk, a.since, a.skip_below)))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
