"""Repositories added by URL, and every index run, as background jobs.

    POST /repos {url}           → clone into CLONE_ROOT, then index
    POST /repos/{name}/refresh  → fetch; re-index only if there is a new commit

Rules:
  * One job runs at a time, server-wide. The summarize and community stages share
    state across repos (communities are computed over the whole graph), so two
    jobs at once would race and pay twice for the same summaries.
  * A user has at most one job queued or running. A repo has at most one.
  * Job state lives on (:Repo) nodes, so every API process and the UI see it. A
    restart cannot finish a job's thread, so jobs it interrupts are marked failed.

A repo URL is untrusted input that makes this server open a network connection, and
the repo it names is untrusted content written to disk. So: https only, to an
allowlist of hosts that resolve to public addresses, never credentials in the URL,
and git runs with impact.py's hardening plus no symlinks, no submodules and no
redirects. The size of a clone, and the number of symbols, are checked before any
LLM call is paid for.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import graph
import impact
import indexer

ACTIVE = ("queued", "running")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_DEFAULT_HOSTS = "github.com,gitlab.com,bitbucket.org"
_GIT_TIMEOUT_S = 900
# Everything else stays blocked by impact._GIT_HARDENING's protocol.allow=never.
_PROTOCOLS = ("https",)


class Busy(Exception):
    """Refused because a job is already queued or running (HTTP 409)."""


class Exists(Exception):
    """This URL was already added (HTTP 409)."""


# --- where repos live -------------------------------------------------------


def repos_root() -> Path:
    """Repos placed on the host and mounted read-only."""
    return Path(os.environ.get("REPOS_ROOT", "/repos")).resolve()


def clone_root() -> Path:
    """Repos cloned from a URL. Writable; only this module writes here."""
    return Path(os.environ.get("CLONE_ROOT", "/clones")).resolve()


def locate(name: str) -> Path | None:
    """The directory for a repo name, from either root, or None.

    Resolving and re-checking the parent rejects a symlink in a root that points
    outside it, as well as any name with a separator or `..` in it."""
    if not _NAME.match(name or ""):
        return None
    for root in (repos_root(), clone_root()):
        path = (root / name).resolve()
        if path.parent == root and path.is_dir():
            return path
    return None


def scrub(message: str) -> str:
    for root in (clone_root(), repos_root()):
        message = message.replace(str(root), "<repos>")
    return message[:400]


# --- URL checks -------------------------------------------------------------


def _allowed_hosts() -> set[str]:
    raw = os.environ.get("CLONE_ALLOWED_HOSTS", _DEFAULT_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _public(host: str) -> bool:
    """Every address the host resolves to is a public one: no loopback, private,
    link-local (cloud metadata) or reserved range. Checked again on each fetch."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global
                                   for a in addresses)


def check_url(url: str) -> tuple[str, str, str]:
    """Validate a repository URL. Returns (normalized url, owner, name).
    Raises ValueError with a message the user can act on."""
    url = (url or "").strip()
    if not url or len(url) > 300 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Enter the repository's https URL, like https://github.com/owner/name")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("Only https URLs are supported")
    if parts.username or parts.password:
        raise ValueError("Remove the user name or token from the URL. Private repositories "
                         "use a token set on the server.")
    if parts.query or parts.fragment:
        raise ValueError("Use the repository's address without ? or #")
    host = (parts.hostname or "").lower()
    if parts.port not in (None, 443):
        raise ValueError("Use the standard https port")
    hosts = _allowed_hosts()
    if "*" not in hosts and host not in hosts:
        raise ValueError(f"Repositories can be added from {', '.join(sorted(hosts))} only")
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
    if len(segments) < 2 or not all(_SEGMENT.match(s) and s not in (".", "..")
                                    for s in segments):
        raise ValueError("Use the repository's address, like https://github.com/owner/name")
    if not _public(host):
        raise ValueError(f"{host} does not resolve to a public address")
    return f"https://{host}/{'/'.join(segments)}.git", segments[-2], segments[-1]


# --- state ------------------------------------------------------------------


def _get(name: str) -> dict[str, Any] | None:
    rows = graph._run("MATCH (r:Repo {name: $name}) RETURN r {.*} AS r", name=name)
    return rows[0]["r"] if rows else None


def _set(name: str, **props: Any) -> None:
    graph._run("MATCH (r:Repo {name: $name}) SET r += $props, r.updated_at = $now",
               name=name, props=props, now=time.time())


def list_repos(user: str) -> list[dict[str, Any]]:
    """Every repo: those added or refreshed here, and any indexed another way
    (the CLI). `mine` marks the caller's own job, so the UI can say why it waits."""
    return graph._run(
        """
        CALL () {
          MATCH (r:Repo) RETURN r.name AS repo
          UNION
          MATCH (s:Symbol) WHERE s.repo IS NOT NULL RETURN DISTINCT s.repo AS repo
        }
        OPTIONAL MATCH (r:Repo {name: repo})
        OPTIONAL MATCH (st:RepoState {repo: repo})
        CALL (repo) { MATCH (s:Symbol {repo: repo}) RETURN count(s) AS symbols }
        RETURN repo, symbols, r.url AS url,
               coalesce(r.status, 'ready') AS status, r.stage AS stage,
               coalesce(r.done, 0) AS done, coalesce(r.total, 0) AS total,
               r.error AS error, r.warning AS warning,
               coalesce(r.skipped_files, 0) AS skipped_files,
               CASE WHEN r.impact_report IS NOT NULL AND r.impact_head = st.commit
                    THEN {report_id: r.impact_report, base: r.impact_base, head: r.impact_head,
                          risk: r.impact_risk, changed: r.impact_changed,
                          affected: r.impact_affected}
               END AS blast,
               st.commit AS commit, st.indexed_at AS indexed_at,
               r.last_checked AS last_checked,
               coalesce(r.job_user = $user AND r.status IN $active, false) AS mine
        ORDER BY repo
        """,
        user=user, active=list(ACTIVE),
    )


def recover() -> None:
    """At startup: a job that was queued or running died with the old process."""
    graph._run(
        """
        MATCH (r:Repo) WHERE r.status IN $active
        SET r.status = 'failed', r.stage = null,
            r.error = 'Stopped by a server restart. Refresh to try again.'
        """,
        active=list(ACTIVE),
    )


# --- queue ------------------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")
_lock = threading.Lock()


def _claim(name: str, user: str, create: dict[str, Any] | None = None,
           analyze: bool = False) -> None:
    """Mark the repo queued for this user, or refuse. The check and the write sit
    under one lock, so two clicks cannot both get through."""
    with _lock:
        busy = graph._run(
            """
            MATCH (r:Repo) WHERE r.status IN $active AND (r.job_user = $user OR r.name = $name)
            RETURN r.name AS name
            """,
            active=list(ACTIVE), user=user, name=name,
        )
        for row in busy:
            if row["name"] == name:
                raise Busy(f"{name} is already being indexed")
            raise Busy(f"You are already indexing {row['name']}. You can start another "
                       f"when it finishes.")
        graph._run(
            """
            MERGE (r:Repo {name: $name})
            ON CREATE SET r += $create
            SET r.status = 'queued', r.stage = null, r.job_user = $user, r.error = null,
                r.done = 0, r.total = 0, r.updated_at = $now, r.analyze_impact = $analyze
            """,
            name=name, create=create or {}, user=user, now=time.time(), analyze=analyze,
        )
    _pool.submit(_job, name)


def add(url: str, user: str) -> str:
    """Queue cloning and indexing a repository URL. Returns the repo's name."""
    url, owner, base = check_url(url)
    existing = graph._run("MATCH (r:Repo {url: $url}) RETURN r.name AS name", url=url)
    if existing:
        raise Exists(f"This repository is already added as {existing[0]['name']}")
    for candidate in (base, f"{owner}-{base}"):
        if _NAME.match(candidate) and _get(candidate) is None and locate(candidate) is None:
            name = candidate
            break
    else:
        raise Exists(f"A repository named {base} already exists")
    _claim(name, user, {"url": url, "added_by": user, "created_at": time.time()})
    return name


def refresh(name: str, user: str, *, analyze: bool = False) -> None:
    """Queue a refresh. A repo from a URL is fetched first; a mounted one is read
    as it is on disk. Either way it is re-indexed only if its commit moved.

    `analyze` (blast radius mode) computes an impact report for the new commit
    against the graph BEFORE re-indexing: the old graph is the only record of what
    depended on the old code, and re-indexing replaces it."""
    if _get(name) is None and locate(name) is None:
        raise LookupError(f"No repository named {name}")
    _claim(name, user, analyze=analyze)


def pending_impact(name: str) -> impact.ImpactReport | None:
    """The impact report for the change that brought the graph to its current
    commit, if that refresh ran in blast radius mode. Otherwise None: a later
    refresh without analysis moved the graph past it, or there never was one."""
    repo = _get(name) or {}
    if not repo.get("impact_report") or repo.get("impact_head") != impact.indexed_commit(name):
        return None
    return impact.load_report(repo["impact_report"])


# --- git --------------------------------------------------------------------


def _git(args: list[str], url: str = "", *, timeout: int = _GIT_TIMEOUT_S) -> str:
    """Run git with the hardening every call here needs. A token, if configured for
    this URL's host, travels in the environment: never on the command line, where
    `ps` shows it, and never into the clone's stored config."""
    env = dict(impact._GIT_ENV)
    token, token_host = os.environ.get("CLONE_TOKEN", ""), os.environ.get("CLONE_TOKEN_HOST", "")
    if token and url and urlsplit(url).hostname == token_host.lower():
        user = os.environ.get("CLONE_TOKEN_USER", "x-access-token")
        credential = base64.b64encode(f"{user}:{token}".encode()).decode()
        env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}"}
    allow = [arg for p in _PROTOCOLS for arg in ("-c", f"protocol.{p}.allow=always")]
    proc = subprocess.run(
        ["git", *impact._GIT_HARDENING, *allow,
         "-c", "core.symlinks=false",          # a link in the repo is checked out as a file
         "-c", "http.followRedirects=false",   # a redirect could point past the host checks
         "-c", "submodule.recurse=false",
         *args],
        capture_output=True, env=env, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0] if args[0] != '-C' else args[2]} failed: "
                           f"{proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout.decode(errors="replace")


def _size_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total / 1_000_000


def _check_size(path: Path) -> None:
    limit = float(os.environ.get("CLONE_MAX_MB", "1000"))
    size = _size_mb(path)
    if size > limit:
        raise RuntimeError(f"The repository is {size:,.0f} MB, over the limit of "
                           f"{limit:,.0f} MB (CLONE_MAX_MB)")


def _clone(url: str, name: str) -> Path:
    """Shallow-clone into a scratch directory, then move it into place, so a
    failed or oversized clone never leaves a half repo behind."""
    root = clone_root()
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=root))
    try:
        src = scratch / "src"
        _git(["clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags",
              "--no-recurse-submodules", "--", url, str(src)], url)
        _check_size(src)
        dest = root / name
        src.rename(dest)
        return dest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _fetch(path: Path, url: str) -> str:
    """Fetch the remote's latest default-branch commit, WITHOUT checking it out.
    Returns its sha. The working tree stays at the indexed commit until _checkout,
    so the change can be analysed against the graph first."""
    check_url(url)  # the allowlist, or the host's address, may have changed
    _git(["-C", str(path), "fetch", "--quiet", "--depth", "1", "--no-tags",
          "--no-recurse-submodules", "origin", "HEAD"], url)
    return _git(["-C", str(path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"]).strip()


def _checkout(path: Path, sha: str) -> None:
    if sha != impact.git_head(str(path)):
        _git(["-C", str(path), "reset", "--quiet", "--hard", sha])
        _check_size(path)


# --- the job ----------------------------------------------------------------


def _has_symbols(name: str) -> bool:
    return bool(graph._run("MATCH (s:Symbol {repo: $repo}) RETURN 1 LIMIT 1", repo=name))


def _complete(name: str) -> bool:
    """Indexed, with every summary written. The commit is recorded when parsing
    ends, so a run that failed while summarizing must not count as up to date."""
    rows = graph._run(
        """
        OPTIONAL MATCH (s:Symbol {repo: $repo})
        WITH count(s) AS symbols,
             count(CASE WHEN s.summary IS NULL OR s.summary = '' THEN 1 END) AS pending
        RETURN symbols > 0 AND pending = 0 AS complete
        """,
        repo=name,
    )
    return bool(rows and rows[0]["complete"])


def _job(name: str) -> None:
    try:
        _set(name, status="running", stage="starting")
        repo = _get(name) or {}
        url = repo.get("url") or ""
        analyze = bool(repo.get("analyze_impact"))
        path = locate(name)
        latest = None
        if url:
            if path is None or not (path / ".git").exists():
                _set(name, stage="cloning")
                path = _clone(url, name)
            else:
                _set(name, stage="fetching")
                latest = _fetch(path, url)
        if path is None:
            raise RuntimeError("The repository's files are gone. Add it again.")

        latest = latest or impact.git_head(str(path))
        indexed = impact.indexed_commit(name)
        if latest and latest == indexed and _complete(name):
            _set(name, status="ready", stage="up to date", last_checked=time.time(), warning=None)
            return

        # The parsed graph is all the analysis needs; missing summaries only make the
        # report's one-line descriptions blank.
        if analyze and indexed and latest and latest != indexed and _has_symbols(name):
            # Before anything changes the graph. If this fails, the job fails and the
            # graph stays as it was, so the analysis can be retried.
            _set(name, stage="analyzing")
            report = impact.analyze(str(path), base=indexed, head=latest, repo=name,
                                    label=f"Refresh {latest[:7]}")
            _set(name, impact_report=report.id, impact_head=latest, impact_base=indexed,
                 impact_risk=report.risk["level"], impact_changed=len(report.changed),
                 impact_affected=len(report.affected))
        if url and latest:
            _checkout(path, latest)

        def progress(stage: str, done: int = 0, total: int = 0) -> None:
            _set(name, stage=stage, done=done, total=total)

        result = indexer.index_repo(str(path), repo=name, progress=progress,
                                    max_symbols=int(os.environ.get("INDEX_MAX_SYMBOLS", "50000")))
        # The graph is usable without every summary, but search is weaker for the
        # missing ones, so say so. Refresh retries exactly those.
        failed = int(result.get("failed", 0))
        _set(name, status="ready", stage="indexed", last_checked=time.time(), done=0, total=0,
             skipped_files=int(result.get("skipped_files", 0)),
             warning=(f"{failed:,} {'summary' if failed == 1 else 'summaries'} failed, so search "
                      f"is weaker for {'that symbol' if failed == 1 else 'those symbols'}. "
                      f"Refresh to retry them.") if failed else None)
    except Exception as exc:  # noqa: BLE001 — the reason is shown to the user
        print(f"[repos] {name} failed: {type(exc).__name__}: {exc}", flush=True)
        _set(name, status="failed", stage=None, error=scrub(str(exc)))
