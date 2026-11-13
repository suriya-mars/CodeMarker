"""Post an impact report on GitHub pull requests.

    GitHub webhook (signed)
      → fetch the pull request's commits into a private mirror
      → impact.analyze(merge-base..head)     no LLM, no tokens
      → one comment on the pull request, updated on each push

Off unless GITHUB_WEBHOOK_SECRET is set. Setup is in RUNBOOK.md.

What leaves the stack: symbol ids, signatures and one-line summaries, sent to the
GitHub repo the code came from. Never source or diffs (impact.to_pr_comment). No LLM
runs here, so a push cannot spend tokens, and nothing a model wrote about this
change reaches the pull request.

A pull request's branch is untrusted input, more so from a fork, where a stranger
writes it. Git runs with impact.py's hardening and only ever reads objects; nothing
is checked out, and files reach the parser under impact's size limit. Forks are
skipped by default because they let anyone on a public repo use this server's disk
and CPU and make it post.

CLI, to try it on one pull request. Prints the comment and posts nothing unless
told to:

    python -m pr_review my-org/my-service 12
    python -m pr_review my-org/my-service 12 --post
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import jwt

import impact

MAX_WEBHOOK_BYTES = 5_000_000
_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
_MAX_QUEUED = 200
_MAX_COMMENT_PAGES = 20
_FETCH_TIMEOUT_S = 900
_API_TIMEOUT_S = 20

_FULL_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOCAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$")

# Everything else stays blocked by impact._GIT_HARDENING's protocol.allow=never.
_FETCH_PROTOCOLS = ("https",)


class QueueFull(Exception):
    pass


# --- configuration ----------------------------------------------------------


def enabled() -> bool:
    return bool(os.environ.get("GITHUB_WEBHOOK_SECRET"))


def repo_map() -> dict[str, str]:
    """{"owner/name": indexed repo name}. Also the allowlist: anything else is ignored."""
    mapping: dict[str, str] = {}
    for item in os.environ.get("GITHUB_REPOS", "").split(","):
        if not item.strip():
            continue
        full, _, local = item.strip().partition("=")
        if not _FULL_NAME.match(full) or not _LOCAL_NAME.match(local):
            raise RuntimeError(f"GITHUB_REPOS entry must be owner/name=indexed-repo, got {item!r}")
        mapping[full.lower()] = local
    return mapping


def check_config() -> None:
    """Fail at startup when half configured, rather than on the first pull request."""
    if not enabled():
        return
    if len(os.environ["GITHUB_WEBHOOK_SECRET"]) < 16:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET must be at least 16 characters")
    if not repo_map():
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is set but GITHUB_REPOS is empty")
    has_app = os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if not has_app and not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is set but no credentials are: set "
                           "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY, or GITHUB_TOKEN")


def _api_url() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _git_url(full_name: str) -> str:
    return f"{os.environ.get('GITHUB_URL', 'https://github.com').rstrip('/')}/{full_name}.git"


def _mirror_root() -> str:
    return os.environ.get("PR_MIRROR_ROOT", "/mirrors")


# --- webhook ----------------------------------------------------------------


def verify_signature(body: bytes, header: str) -> bool:
    """Is this body signed with our secret? Fails closed without a secret."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pr-review")
_queued: set[tuple[str, int]] = set()
_queued_lock = threading.Lock()


def handle_event(event: str, body: bytes) -> str:
    """Decide from a verified webhook event whether to review. Returns what happened."""
    if event != "pull_request":
        return "ignored"
    payload = json.loads(body)
    full_name = str(payload.get("repository", {}).get("full_name", ""))
    number = payload.get("pull_request", {}).get("number")
    if payload.get("action") not in _ACTIONS or not isinstance(number, int):
        return "ignored"
    if full_name.lower() not in repo_map():
        print(f"[pr] ignoring {full_name!r}: not listed in GITHUB_REPOS")
        return "ignored"
    return "queued" if enqueue(full_name, number) else "already queued"


def enqueue(full_name: str, number: int) -> bool:
    """Reviews run one at a time, so they never fight over a mirror or the CPU that
    answers questions. A pull request already waiting is not queued twice: its job
    reads the latest commit when it runs, so pushes in quick succession cost one."""
    key = (full_name.lower(), number)
    with _queued_lock:
        if key in _queued:
            return False
        if len(_queued) >= _MAX_QUEUED:
            raise QueueFull
        _queued.add(key)
    _pool.submit(_job, full_name, number)
    return True


def _job(full_name: str, number: int) -> None:
    with _queued_lock:
        _queued.discard((full_name.lower(), number))
    try:
        review(full_name, number)
    except Exception as exc:  # noqa: BLE001 — one bad pull request must not stop the queue
        print(f"[pr] {full_name}#{number} failed: {type(exc).__name__}: {exc}", flush=True)


# --- GitHub API -------------------------------------------------------------


def _request(method: str, path: str, *, token: str = "", bearer: str = "",
             body: dict[str, Any] | None = None) -> Any:
    credential = bearer or token
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RepoDigger",
               "X-GitHub-Api-Version": "2022-11-28"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_api_url()}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_S) as resp:  # noqa: S310 — operator-set URL
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        # the status and path only: the response body is not needed, and headers hold the token
        raise RuntimeError(f"GitHub {method} {path} returned {exc.code}") from None


def _app_jwt() -> str:
    now = int(time.time())
    key = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n")
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]},
                      key, algorithm="RS256")


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def _token(full_name: str) -> str:
    """A token for this repo only, with only the two permissions this module uses.
    An installation can cover a whole organisation; a leak here must not."""
    if os.environ.get("GITHUB_TOKEN") and not os.environ.get("GITHUB_APP_ID"):
        return os.environ["GITHUB_TOKEN"]
    with _tokens_lock:
        token, expires = _tokens.get(full_name, ("", 0.0))
        if time.time() < expires:
            return token
        app = _app_jwt()
        install = _request("GET", f"/repos/{full_name}/installation", bearer=app)
        issued = _request(
            "POST", f"/app/installations/{install['id']}/access_tokens", bearer=app,
            body={"repositories": [full_name.split("/", 1)[1]],
                  "permissions": {"contents": "read", "pull_requests": "write"}},
        )
        # installation tokens last an hour; refresh well before that
        _tokens[full_name] = (issued["token"], time.time() + 45 * 60)
        return issued["token"]


def post_comment(full_name: str, number: int, body: str, token: str) -> None:
    """Update our comment on the pull request, or add it. One comment that stays
    current, rather than one per push."""
    path = f"/repos/{full_name}/issues/{number}/comments"
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        comments = _request("GET", f"{path}?per_page=100&page={page}", token=token)
        for c in comments:
            if str(c.get("body", "")).startswith(impact.PR_COMMENT_MARKER):
                _request("PATCH", f"/repos/{full_name}/issues/comments/{int(c['id'])}",
                         token=token, body={"body": body})
                return
        if len(comments) < 100:
            break
    _request("POST", path, token=token, body={"body": body})


# --- git --------------------------------------------------------------------


def _fetch(full_name: str, local: str, number: int, base_ref: str, token: str) -> tuple[str, str]:
    """Fetch the pull request and its target branch into a mirror nobody else writes.

    Returns (base tip, head). A separate mirror because the indexed checkout is mounted
    read-only. The token goes through the environment, never the command line, where
    `ps` would show it, and never into a stored config or URL.
    """
    if not _BRANCH.match(base_ref) or ".." in base_ref:
        raise RuntimeError("unsupported base branch name")
    mirror = os.path.join(_mirror_root(), local)
    os.makedirs(mirror, exist_ok=True)
    allow = [arg for proto in _FETCH_PROTOCOLS for arg in ("-c", f"protocol.{proto}.allow=always")]
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = impact._GIT_ENV | {"GIT_CONFIG_COUNT": "1",
                             "GIT_CONFIG_KEY_0": "http.extraHeader",
                             "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}"}
    git = ["git", *impact._GIT_HARDENING, *allow, "-C", mirror]
    # Not bare: impact reads files as "rev:./path", which needs a work tree. It stays
    # empty; nothing is ever checked out.
    if not os.path.isdir(os.path.join(mirror, ".git")):
        subprocess.run([*git, "init", "--quiet"], check=True, capture_output=True, env=env, timeout=30)
    base_local, head_local = f"refs/rd/{number}/base", f"refs/rd/{number}/head"
    proc = subprocess.run(
        [*git, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", _git_url(full_name),
         f"+refs/heads/{base_ref}:{base_local}", f"+refs/pull/{number}/head:{head_local}"],
        capture_output=True, env=env, timeout=_FETCH_TIMEOUT_S, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git fetch failed: {proc.stderr.decode(errors='replace').strip()[:500]}")
    return impact._resolve(mirror, base_local), impact._resolve(mirror, head_local)


# --- the review -------------------------------------------------------------


_FAILED = (f"{impact.PR_COMMENT_MARKER}\n## RepoDigger impact: not available\n\n"
           "RepoDigger could not analyze commit {sha}. The reason is in the API logs. Any "
           "earlier report no longer applies to this pull request.\n")
_TOO_LARGE = (f"{impact.PR_COMMENT_MARKER}\n## RepoDigger impact: not available\n\n"
              "This pull request changes {files} files, over the limit of {limit}, so it was "
              "not analyzed.\n")


def _report_url(report: impact.ImpactReport) -> str:
    public = os.environ.get("PUBLIC_URL", "").rstrip("/")
    return f"{public}/?repo={report.repo}&report={report.id}" if public else ""


def review(full_name: str, number: int, *, post: bool = True) -> str | None:
    """Analyze one pull request and return the comment. None if it was skipped."""
    local = repo_map().get(full_name.lower())
    if local is None:
        raise RuntimeError(f"{full_name} is not listed in GITHUB_REPOS")
    token = _token(full_name)
    pr = _request("GET", f"/repos/{full_name}/pulls/{int(number)}", token=token)
    head_repo = (pr["head"].get("repo") or {}).get("full_name", "")
    if head_repo.lower() != full_name.lower() and os.environ.get("GITHUB_PR_FORKS", "0") != "1":
        print(f"[pr] {full_name}#{number}: from a fork, skipped (GITHUB_PR_FORKS=0)")
        return None

    max_files = int(os.environ.get("PR_MAX_FILES", "2000"))
    head = str(pr["head"]["sha"])[:10]
    try:
        base_tip, head = _fetch(full_name, local, number, pr["base"]["ref"], token)
        mirror = os.path.join(_mirror_root(), local)
        base = impact.merge_base(mirror, base_tip, head)
        files = len(impact.changed_files(mirror, base, head))
        if files > max_files:
            comment = _TOO_LARGE.format(files=files, limit=max_files)
        else:
            report = impact.analyze(mirror, base=base, head=head, repo=local,
                                    label=f"PR #{int(number)}")
            impact.replace_labelled_reports(report)
            comment = impact.to_pr_comment(report, _report_url(report))
    except Exception as exc:  # noqa: BLE001 — the old comment must not keep claiming to be current
        print(f"[pr] {full_name}#{number} could not be analyzed: {exc}", flush=True)
        comment = _FAILED.format(sha=impact._code(head[:10]))
    if post:
        post_comment(full_name, number, comment, token)
        print(f"[pr] {full_name}#{number}: comment updated at {head[:10]}", flush=True)
    return comment


def main() -> None:
    import argparse

    import graph

    ap = argparse.ArgumentParser(prog="python -m pr_review",
                                 description="Analyze one pull request and print the comment.")
    ap.add_argument("repository", help="owner/name, as listed in GITHUB_REPOS")
    ap.add_argument("number", type=int)
    ap.add_argument("--post", action="store_true", help="also post it to the pull request")
    args = ap.parse_args()
    print(review(args.repository, args.number, post=args.post) or "(skipped)")
    graph.close_driver()


if __name__ == "__main__":
    main()
