"""Pull request comments, against a fake GitHub on localhost and a real git remote.

Nothing here reaches github.com. The integration test runs the production sequence:
index the first commit, open a pull request, and check what gets posted.
"""

import hashlib
import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import graph
import impact
import indexer
import pr_review
import summarize

SECRET = "webhook-secret-for-tests"
URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event(action="opened", repo="org/svc", number=7) -> bytes:
    return json.dumps({"action": action, "number": number,
                       "pull_request": {"number": number},
                       "repository": {"full_name": repo}}).encode()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_REPOS", "org/svc=svc")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)


def _client(monkeypatch, auth_mode="token"):
    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("API_TOKEN", "x" * 32)
    import main

    return TestClient(main.app)


# --- webhook gate -----------------------------------------------------------


def test_webhook_is_absent_without_a_secret(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    r = _client(monkeypatch).post("/webhooks/github", content=b"{}")
    assert r.status_code == 404


@pytest.mark.parametrize("signature", ["", "sha256=", "sha256=00", "sha1=abc", _sign(b"other")])
def test_webhook_rejects_a_bad_signature(monkeypatch, configured, signature):
    seen = []
    monkeypatch.setattr(pr_review, "enqueue", lambda *a: seen.append(a))
    r = _client(monkeypatch).post("/webhooks/github", content=_event(),
                                  headers={"x-github-event": "pull_request",
                                           "x-hub-signature-256": signature})
    assert r.status_code == 401
    assert seen == []


def test_webhook_rejects_the_wrong_secret(monkeypatch, configured):
    body = _event()
    r = _client(monkeypatch).post("/webhooks/github", content=body,
                                  headers={"x-github-event": "pull_request",
                                           "x-hub-signature-256": _sign(body, "other-secret-value")})
    assert r.status_code == 401


def test_a_signature_is_not_a_login_to_anything_else(monkeypatch, configured):
    """The webhook path is open to login checks; no other route may be."""
    assert _client(monkeypatch).get("/impact").status_code == 401


def test_oversized_payload_is_refused_before_checking_it(monkeypatch, configured):
    monkeypatch.setattr(pr_review, "MAX_WEBHOOK_BYTES", 10)
    r = _client(monkeypatch).post("/webhooks/github", content=b"x" * 11)
    assert r.status_code == 413


@pytest.mark.parametrize(("event", "body", "expected"), [
    ("ping", b"{}", "ignored"),
    ("push", _event(), "ignored"),
    ("pull_request", _event(action="closed"), "ignored"),
    ("pull_request", _event(repo="stranger/repo"), "ignored"),
    ("pull_request", _event(), "queued"),
    ("pull_request", _event(action="synchronize"), "queued"),
])
def test_webhook_only_reviews_listed_repos_and_relevant_actions(monkeypatch, configured,
                                                                event, body, expected):
    monkeypatch.setattr(pr_review, "enqueue", lambda *a: True)
    r = _client(monkeypatch).post("/webhooks/github", content=body,
                                  headers={"x-github-event": event,
                                           "x-hub-signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json() == {"status": expected}


def test_pushes_in_quick_succession_queue_once(monkeypatch, configured):
    ran = []
    monkeypatch.setattr(pr_review, "_pool",
                        type("P", (), {"submit": lambda self, *a: ran.append(a)})())
    pr_review._queued.clear()
    assert pr_review.enqueue("org/svc", 7) is True
    assert pr_review.enqueue("ORG/svc", 7) is False
    assert pr_review.enqueue("org/svc", 8) is True
    assert len(ran) == 2
    pr_review._queued.clear()


def test_full_queue_is_a_503_so_github_shows_the_failed_delivery(monkeypatch, configured):
    def full(*a):
        raise pr_review.QueueFull

    monkeypatch.setattr(pr_review, "enqueue", full)
    body = _event()
    r = _client(monkeypatch).post("/webhooks/github", content=body,
                                  headers={"x-github-event": "pull_request",
                                           "x-hub-signature-256": _sign(body)})
    assert r.status_code == 503


@pytest.mark.parametrize(("env", "message"), [
    ({"GITHUB_WEBHOOK_SECRET": "short"}, "16 characters"),
    ({"GITHUB_REPOS": ""}, "GITHUB_REPOS"),
    ({"GITHUB_TOKEN": ""}, "credentials"),
    ({"GITHUB_REPOS": "no-slash=svc"}, "owner/name"),
    ({"GITHUB_REPOS": "org/svc=../../etc"}, "owner/name"),
])
def test_half_configured_refuses_to_start(monkeypatch, configured, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match=message):
        pr_review.check_config()


def test_off_by_default_needs_no_configuration(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    pr_review.check_config()


# --- comment content --------------------------------------------------------


def _report(**kw):
    base = {"id": "r1", "repo": "svc", "base": "a" * 40, "head": "b" * 40, "created_at": 0.0,
            "files": [{"status": "M", "path": "core.py", "old_path": "core.py"}],
            "changed": [], "affected": [], "importers": [], "communities": [],
            "risk": {"level": "low", "score": 0, "flags": [], "affected_pages": 0,
                     "affected_tests": 0},
            "warnings": []}
    return impact.ImpactReport(**(base | kw))


def _changed(i=0, summary="", **kw):
    return impact.ChangedSymbol(id=f"core.f{i}", kind="function", name=f"f{i}", path="core.py",
                                change=kw.pop("change", "modified"), summary=summary, **kw)


def _affected(i=0, summary="does things"):
    return impact.AffectedSymbol(id=f"core.caller{i}", kind="function", name="c", path="core.py",
                                 summary=summary, distance=1, via="core.f0", edge="CALLS",
                                 path_to_change=[f"core.caller{i}", "core.f0"])


def test_comment_starts_with_the_marker_it_is_found_by():
    assert impact.to_pr_comment(_report()).startswith(impact.PR_COMMENT_MARKER)


def test_comment_never_carries_source_or_diffs():
    secret = "api_key = load_the_secret()"
    r = _report(changed=[_changed(diff=f"-{secret}\n+pass")])
    assert "load_the_secret" not in impact.to_pr_comment(r)


@pytest.mark.parametrize("attack", [
    "@victim please approve this",
    "fixes #12 and owner/repo#3",
    "visit https://evil.example/login now",
    "go to www.evil.example today",
    "![x](https://evil.example/?d=1)",
    "[click](https://evil.example)",
    "</details><script>alert(1)</script>",
    "`code` escape ``` fence",
])
def test_repo_text_in_a_comment_cannot_notify_link_or_inject(attack):
    r = _report(affected=[_affected(summary=attack)],
                communities=[{"id": "c0", "name": "c", "summary": attack, "affected": 1,
                              "contains_change": True}],
                warnings=[attack])
    body = impact.to_pr_comment(r)
    assert "@victim" not in body
    assert "#12" not in body and "repo#3" not in body
    assert "https://evil" not in body and "www.evil" not in body
    assert "<script>" not in body
    assert "![x](" not in body and "[click](" not in body
    assert body.count("</details>") == body.count("<details>")


def test_a_huge_blast_radius_drops_detail_rather_than_failing_on_github():
    r = _report(changed=[_changed(i, summary="s" * 240) for i in range(500)],
                affected=[_affected(i, summary="x" * 2000) for i in range(500)],
                communities=[{"id": f"c{i}", "name": "n" * 500, "summary": "y" * 2000,
                              "affected": 1, "contains_change": False} for i in range(200)])
    body = impact.to_pr_comment(r)
    assert len(body) <= 60_000
    assert body.count("</details>") == body.count("<details>")
    assert "and 470 more" in body  # says what it left out


def test_an_oversized_signature_is_shortened():
    flag = {"level": "high", "code": "X", "message": "m" * 5000, "dependents": ["d"] * 40}
    r = _report(risk={"level": "high", "score": 1, "flags": [flag] * 60, "affected_pages": 0,
                      "affected_tests": 0})
    body = impact.to_pr_comment(r)
    assert "m" * 301 not in body
    assert "and 40 more" in body and "and 30 more" in body


def test_report_link_only_when_a_public_url_is_set(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert pr_review._report_url(_report()) == ""
    monkeypatch.setenv("PUBLIC_URL", "https://rd.example/")
    assert pr_review._report_url(_report()) == "https://rd.example/?repo=svc&report=r1"


# --- fake GitHub ------------------------------------------------------------


class FakeGitHub:
    def __init__(self):
        self.calls, self.comments, self.pr, self.jwts = [], [], {}, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                auth = self.headers.get("Authorization", "")
                outer.calls.append((self.command, self.path, auth, body))
                path = self.path.split("?")[0]
                if path.endswith("/installation"):
                    outer.jwts.append(auth.removeprefix("Bearer "))
                    return self._send(200, {"id": 99})
                if path.endswith("/access_tokens"):
                    return self._send(201, {"token": "ghs_issued"})
                if "/pulls/" in path:
                    return self._send(200, outer.pr)
                if path.endswith("/comments") and self.command == "GET":
                    return self._send(200, outer.comments)
                if path.endswith("/comments"):
                    outer.comments.append({"id": len(outer.comments) + 1, "body": body["body"]})
                    return self._send(201, {})
                if "/comments/" in path:
                    cid = int(path.rsplit("/", 1)[1])
                    next(c for c in outer.comments if c["id"] == cid)["body"] = body["body"]
                    return self._send(200, {})
                return self._send(404, {})

            do_GET = do_POST = do_PATCH = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setenv("GITHUB_API_URL", fake.url)
    yield fake
    fake.close()


def test_comment_is_updated_not_duplicated(github):
    pr_review.post_comment("org/svc", 7, f"{impact.PR_COMMENT_MARKER}\nfirst", "t")
    pr_review.post_comment("org/svc", 7, f"{impact.PR_COMMENT_MARKER}\nsecond", "t")
    assert len(github.comments) == 1
    assert github.comments[0]["body"].endswith("second")


def test_other_peoples_comments_are_left_alone(github):
    github.comments.append({"id": 1, "body": "looks good to me"})
    pr_review.post_comment("org/svc", 7, f"{impact.PR_COMMENT_MARKER}\nreport", "t")
    assert github.comments[0]["body"] == "looks good to me"
    assert len(github.comments) == 2


def test_app_token_is_narrowed_to_one_repo_and_two_permissions(monkeypatch, github):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "1234")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))
    pr_review._tokens.clear()

    assert pr_review._token("org/svc") == "ghs_issued"
    assert pr_review._token("org/svc") == "ghs_issued"  # cached: one exchange
    claims = jwt.decode(github.jwts[0], key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "1234"
    issued = [c for c in github.calls if c[1].endswith("/access_tokens")]
    assert len(issued) == 1
    assert issued[0][3] == {"repositories": ["svc"],
                            "permissions": {"contents": "read", "pull_requests": "write"}}
    pr_review._tokens.clear()


def test_github_errors_do_not_expose_the_token(github):
    with pytest.raises(RuntimeError) as err:
        pr_review._request("GET", "/nope", token="ghs_supersecret")
    assert "ghs_supersecret" not in str(err.value)


@pytest.mark.parametrize("branch", ["-upload-pack=evil", "a..b", "a b", ""])
def test_a_hostile_base_branch_never_reaches_git(tmp_path, monkeypatch, branch):
    monkeypatch.setenv("PR_MIRROR_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="base branch"):
        pr_review._fetch("org/svc", "svc", 1, branch, "t")
    assert list(tmp_path.iterdir()) == []


# --- end to end -------------------------------------------------------------


def _reachable() -> bool:
    if not URI or not PASSWORD:
        return False
    try:
        from neo4j import GraphDatabase

        d = GraphDatabase.driver(URI, auth=("neo4j", PASSWORD))
        d.verify_connectivity()
        d.close()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_neo4j = pytest.mark.skipif(not _reachable(), reason="no Neo4j at NEO4J_TEST_URI")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
                   check=True, capture_output=True)


V1 = "def load(path):\n    return path\n\n\ndef run():\n    return load('a')\n\n\ndef other():\n    return 1\n"
V2 = V1.replace("def load(path):", "def load(path, strict):")


@pytest.fixture
def world(tmp_path, monkeypatch, github, configured):
    """A remote with a pull request, and the graph indexed at the base commit."""
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()
    graph.setup_schema()
    graph.wipe_test_database()
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"summary of {s.name}")
    monkeypatch.setattr(summarize, "summarize_community", lambda n, m: f"cluster {n}")

    work = tmp_path / "remote" / "org" / "svc.git"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "core.py").write_text(V1)
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    indexer.index_repo(str(work), repo="svc")

    _git(work, "checkout", "-qb", "feature")
    (work / "core.py").write_text(V2)
    _git(work, "commit", "-qam", "change")
    _git(work, "update-ref", "refs/pull/7/head", "feature")
    _git(work, "checkout", "-q", "main")

    monkeypatch.setenv("GITHUB_URL", f"file://{tmp_path}/remote")
    monkeypatch.setenv("PR_MIRROR_ROOT", str(tmp_path / "mirrors"))
    monkeypatch.setattr(pr_review, "_FETCH_PROTOCOLS", ("file",))
    github.pr = {"head": {"sha": "unused", "repo": {"full_name": "org/svc"}},
                 "base": {"ref": "main"}}
    yield work, github
    graph.close_driver()


@needs_neo4j
def test_pull_request_gets_a_comment_naming_the_broken_caller(world):
    _, gh = world
    body = pr_review.review("org/svc", 7)

    assert len(gh.comments) == 1 and gh.comments[0]["body"] == body
    assert "SIGNATURE\\_CHANGED" in body or "SIGNATURE_CHANGED_CALLERS_UNTOUCHED" in body
    assert "core.run" in body and "core.load" in body
    assert "core.other" not in body
    assert "return path" not in body  # no source
    rows = impact.list_reports("svc")
    assert [r["label"] for r in rows] == ["PR #7"]


@needs_neo4j
def test_each_push_updates_the_comment_and_keeps_one_report(world):
    work, gh = world
    pr_review.review("org/svc", 7)
    _git(work, "checkout", "-q", "feature")
    (work / "core.py").write_text(V2 + "\n\ndef extra():\n    return 2\n")
    _git(work, "commit", "-qam", "more")
    _git(work, "update-ref", "refs/pull/7/head", "feature")
    _git(work, "checkout", "-q", "main")

    pr_review.review("org/svc", 7)
    assert len(gh.comments) == 1
    assert "core.extra" in gh.comments[0]["body"]
    assert len(impact.list_reports("svc")) == 1


@needs_neo4j
def test_later_changes_on_main_are_not_blamed_on_the_pull_request(world):
    """The graph is re-indexed past where the branch started. The other change must
    not appear as if this pull request undid it."""
    work, gh = world
    _git(work, "checkout", "-q", "main")
    (work / "core.py").write_text(V1.replace("return 1", "return 99"))
    _git(work, "commit", "-qam", "someone else's change on main")
    indexer.index_repo(str(work), repo="svc")

    body = pr_review.review("org/svc", 7)
    assert "core.other" not in body  # untouched by this pull request
    assert "core.load" in body
    assert "graph was indexed at" in body  # and it says the graph is not at the base


@needs_neo4j
def test_a_fork_is_skipped_by_default_and_reviewed_when_allowed(world, monkeypatch):
    _, gh = world
    gh.pr["head"]["repo"] = {"full_name": "stranger/svc"}
    assert pr_review.review("org/svc", 7) is None
    assert gh.comments == []

    monkeypatch.setenv("GITHUB_PR_FORKS", "1")
    assert pr_review.review("org/svc", 7) is not None
    assert len(gh.comments) == 1


@needs_neo4j
def test_a_deleted_fork_is_treated_as_a_fork(world):
    _, gh = world
    gh.pr["head"]["repo"] = None
    assert pr_review.review("org/svc", 7) is None


@needs_neo4j
def test_failure_replaces_the_old_report_instead_of_leaving_it_current(world, monkeypatch):
    _, gh = world
    pr_review.review("org/svc", 7)
    assert "SIGNATURE" in gh.comments[0]["body"]

    def boom(*a, **k):
        raise RuntimeError("/mirrors/svc secret path detail")

    monkeypatch.setattr(impact, "analyze", boom)
    pr_review.review("org/svc", 7)
    body = gh.comments[0]["body"]
    assert len(gh.comments) == 1
    assert "could not analyze" in body and "no longer applies" in body
    assert "secret path detail" not in body


@needs_neo4j
def test_too_many_files_is_said_plainly(world, monkeypatch):
    _, gh = world
    monkeypatch.setenv("PR_MAX_FILES", "0")
    pr_review.review("org/svc", 7)
    assert "over the limit of 0" in gh.comments[0]["body"]


@needs_neo4j
def test_only_https_is_allowed_outside_tests(world, monkeypatch):
    monkeypatch.setattr(pr_review, "_FETCH_PROTOCOLS", ("https",))
    with pytest.raises(RuntimeError, match="git fetch failed"):
        pr_review._fetch("org/svc", "svc", 7, "main", "t")


@needs_neo4j
def test_the_token_is_not_written_to_the_mirror(world, tmp_path):
    pr_review.review("org/svc", 7)
    config = (tmp_path / "mirrors" / "svc" / ".git" / "config").read_text()
    assert "x-access-token" not in config and "Authorization" not in config
    assert not (tmp_path / "mirrors" / "svc" / "core.py").exists()  # nothing checked out


@needs_neo4j
def test_community_summaries_are_reused_across_runs(world, monkeypatch):
    """Lives here for the fixture: a real index, rerun with nothing changed."""
    paid = []
    monkeypatch.setattr(summarize, "summarize_community",
                        lambda n, m: paid.append(n) or f"cluster {n}")
    stats = indexer.index_repo(str(world[0]), repo="svc", stages=("community",))
    assert paid == []
    assert stats["community_summaries_generated"] == 0
    assert stats["community_summaries_reused"] == stats["communities"]
