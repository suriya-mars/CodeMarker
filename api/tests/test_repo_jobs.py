"""Repositories added by URL, refreshed, and indexed as background jobs.

Nothing reaches the network: clones come from a local git remote, and DNS is
replaced where the URL checks need it.
"""

import os
import subprocess
import threading

import pytest

import graph
import impact
import indexer
import repo_jobs
import summarize

URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(repo_jobs, "_public", lambda host: True)


# --- URL checks -------------------------------------------------------------


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/psf/requests", ("https://github.com/psf/requests.git", "psf", "requests")),
    ("https://github.com/psf/requests.git", ("https://github.com/psf/requests.git", "psf", "requests")),
    ("  https://GitHub.com/psf/requests/  ", ("https://github.com/psf/requests.git", "psf", "requests")),
    ("https://gitlab.com/group/sub/proj", ("https://gitlab.com/group/sub/proj.git", "sub", "proj")),
])
def test_ordinary_urls_are_accepted(public_dns, url, expected):
    assert repo_jobs.check_url(url) == expected


@pytest.mark.parametrize(("url", "message"), [
    ("http://github.com/a/b", "https"),
    ("git@github.com:a/b.git", "https"),
    ("ssh://github.com/a/b", "https"),
    ("file:///etc/passwd", "https"),
    ("ext::sh -c touch% /tmp/x", "https"),
    ("https://user:ghp_secret@github.com/a/b", "token"),
    ("https://evil.example/a/b", "github.com"),
    ("https://github.com.evil.example/a/b", "github.com"),
    ("https://github.com:8443/a/b", "port"),
    ("https://github.com/a", "address"),
    ("https://github.com/../etc", "address"),
    ("https://github.com/a/b?x=1", "without"),
    ("https://github.com/a/b c", "Enter"),
    ("https://github.com/a\n/b", "Enter"),
    ("", "Enter"),
    ("https://github.com/" + "a" * 300, "Enter"),
    ("https://github.com/-upload-pack=x/b", "address"),
])
def test_hostile_or_unsupported_urls_are_refused(public_dns, url, message):
    with pytest.raises(ValueError, match=message):
        repo_jobs.check_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1",
                                     "192.168.1.1", "100.64.0.1"])
def test_a_host_resolving_to_a_private_address_is_refused(monkeypatch, address):
    """Cloud metadata and internal services, reached by an allowed name."""
    monkeypatch.setenv("CLONE_ALLOWED_HOSTS", "*")
    monkeypatch.setattr(repo_jobs.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, (address, 443))])
    with pytest.raises(ValueError, match="public address"):
        repo_jobs.check_url("https://git.internal.example/a/b")


def test_one_private_address_among_public_ones_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(repo_jobs.socket, "getaddrinfo", lambda *a, **k: [
        (None, None, None, None, ("140.82.112.3", 443)),
        (None, None, None, None, ("10.0.0.1", 443))])
    assert repo_jobs._public("github.com") is False


def test_the_allowlist_is_configurable(monkeypatch, public_dns):
    monkeypatch.setenv("CLONE_ALLOWED_HOSTS", "git.acme.example")
    assert repo_jobs.check_url("https://git.acme.example/team/app")[2] == "app"
    with pytest.raises(ValueError):
        repo_jobs.check_url("https://github.com/a/b")


# --- locating ---------------------------------------------------------------


def test_locate_finds_mounted_and_cloned_and_nothing_outside(tmp_path, monkeypatch):
    mounted, cloned = tmp_path / "repos", tmp_path / "clones"
    (mounted / "shop").mkdir(parents=True)
    (cloned / "lib").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", cloned / "escape")
    monkeypatch.setenv("REPOS_ROOT", str(mounted))
    monkeypatch.setenv("CLONE_ROOT", str(cloned))
    assert repo_jobs.locate("shop") == mounted / "shop"
    assert repo_jobs.locate("lib") == cloned / "lib"
    for bad in ("escape", "..", "../secret", "a/b", "", "missing"):
        assert repo_jobs.locate(bad) is None


# --- with a database --------------------------------------------------------


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


V1 = "def load(path):\n    return path\n\n\ndef run():\n    return load('a')\n"


class Inline:
    """Runs submitted jobs on the caller's thread, so tests see the result."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def world(tmp_path, monkeypatch):
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()
    graph.setup_schema()
    graph.wipe_test_database()
    monkeypatch.setenv("REPOS_ROOT", str(tmp_path / "repos"))
    monkeypatch.setenv("CLONE_ROOT", str(tmp_path / "clones"))
    (tmp_path / "repos").mkdir()
    paid = []
    monkeypatch.setattr(summarize, "summarize_symbol",
                        lambda s: paid.append(s.id) or f"does {s.name}")
    monkeypatch.setattr(summarize, "summarize_community", lambda n, m: "a cluster")
    monkeypatch.setattr(repo_jobs, "_pool", Inline())
    monkeypatch.setattr(repo_jobs, "_PROTOCOLS", ("file",))
    monkeypatch.setattr(repo_jobs, "check_url",
                        lambda url: (url, "acme", url.rstrip("/").split("/")[-1].removesuffix(".git")))

    remote = tmp_path / "remote" / "shop.git"
    remote.mkdir(parents=True)
    _git(remote, "init", "-q", "-b", "main")
    (remote / "core.py").write_text(V1)
    _git(remote, "add", ".")
    _git(remote, "commit", "-qm", "first")
    yield {"remote": remote, "url": f"file://{remote}", "paid": paid, "tmp": tmp_path}
    graph.close_driver()


def _row(name, user="alice"):
    return next(r for r in repo_jobs.list_repos(user) if r["repo"] == name)


@needs_neo4j
def test_adding_a_url_clones_and_indexes(world):
    name = repo_jobs.add(world["url"], "alice")
    row = _row(name)
    assert name == "shop"
    assert (row["status"], row["stage"], row["symbols"]) == ("ready", "indexed", 3)
    assert row["commit"] == impact.git_head(str(world["remote"]))
    assert row["url"] == world["url"] and row["mine"] is False
    assert (world["tmp"] / "clones" / "shop" / "core.py").exists()


@needs_neo4j
def test_refresh_with_no_new_commit_does_no_work(world, monkeypatch):
    repo_jobs.add(world["url"], "alice")
    ran = []
    monkeypatch.setattr(indexer, "index_repo", lambda *a, **k: ran.append(a))
    repo_jobs.refresh("shop", "bob")
    assert ran == []
    row = _row("shop")
    assert (row["status"], row["stage"]) == ("ready", "up to date")


@needs_neo4j
def test_refresh_with_a_new_commit_reindexes_and_pays_only_for_changes(world):
    repo_jobs.add(world["url"], "alice")
    world["paid"].clear()
    (world["remote"] / "core.py").write_text(V1 + "\n\ndef extra():\n    return 1\n")
    _git(world["remote"], "commit", "-qam", "second")

    repo_jobs.refresh("shop", "alice")
    row = _row("shop")
    assert (row["status"], row["symbols"]) == ("ready", 4)
    assert row["commit"] == impact.git_head(str(world["remote"]))
    # only new or changed code is paid for; the untouched functions are not
    assert "core.extra" in world["paid"]
    assert "core.load" not in world["paid"] and "core.run" not in world["paid"]


@needs_neo4j
def test_a_mounted_repo_can_be_refreshed_too(world):
    mounted = world["tmp"] / "repos" / "local"
    mounted.mkdir()
    (mounted / "m.py").write_text("def f():\n    return 1\n")
    repo_jobs.refresh("local", "alice")
    assert _row("local")["symbols"] == 2
    with pytest.raises(LookupError):
        repo_jobs.refresh("nothing-here", "alice")


@needs_neo4j
def test_one_job_per_user_and_per_repo(world, monkeypatch):
    held = []
    monkeypatch.setattr(repo_jobs, "_pool", type("P", (), {"submit": lambda s, *a: held.append(a)})())
    repo_jobs.add(world["url"], "alice")  # queued, not run
    with pytest.raises(repo_jobs.Busy, match="shop is already being indexed"):
        repo_jobs.refresh("shop", "alice")
    other = world["tmp"] / "repos" / "other"
    other.mkdir()
    with pytest.raises(repo_jobs.Busy, match="already indexing shop"):
        repo_jobs.refresh("other", "alice")  # a second repo, same user
    with pytest.raises(repo_jobs.Busy, match="shop is already being indexed"):
        repo_jobs.refresh("shop", "bob")  # same repo, another user
    repo_jobs.refresh("other", "bob")  # another user, another repo: queued
    assert len(held) == 2
    assert _row("shop", "alice")["mine"] is True and _row("shop", "bob")["mine"] is False


@needs_neo4j
def test_two_clicks_at_once_queue_one_job(world, monkeypatch):
    held = []
    monkeypatch.setattr(repo_jobs, "_pool", type("P", (), {"submit": lambda s, *a: held.append(a)})())
    (world["tmp"] / "repos" / "other").mkdir()
    errors = []

    def click():
        try:
            repo_jobs.refresh("other", "alice")
        except repo_jobs.Busy as exc:
            errors.append(exc)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(held) == 1 and len(errors) == 7


@needs_neo4j
def test_the_same_url_twice_is_refused(world):
    repo_jobs.add(world["url"], "alice")
    with pytest.raises(repo_jobs.Exists, match="already added as shop"):
        repo_jobs.add(world["url"], "bob")


@needs_neo4j
def test_a_name_clash_takes_the_owners_name(world):
    (world["tmp"] / "repos" / "shop").mkdir()  # a mounted repo already called shop
    assert repo_jobs.add(world["url"], "alice") == "acme-shop"


@needs_neo4j
def test_a_failed_clone_leaves_nothing_behind(world):
    name = repo_jobs.add(f"file://{world['tmp']}/remote/missing.git", "alice")
    row = _row(name)
    assert row["status"] == "failed" and "git clone failed" in row["error"]
    assert str(world["tmp"] / "clones") not in row["error"]  # our paths are scrubbed
    assert [p for p in (world["tmp"] / "clones").iterdir()] == []


@needs_neo4j
def test_an_oversized_repo_is_refused_and_removed(world, monkeypatch):
    monkeypatch.setenv("CLONE_MAX_MB", "0")
    name = repo_jobs.add(world["url"], "alice")
    row = _row(name)
    assert row["status"] == "failed" and "CLONE_MAX_MB" in row["error"]
    assert not (world["tmp"] / "clones" / "shop").exists()
    assert world["paid"] == []


@needs_neo4j
def test_too_many_symbols_is_refused_before_any_summary_is_paid_for(world, monkeypatch):
    monkeypatch.setenv("INDEX_MAX_SYMBOLS", "2")
    repo_jobs.add(world["url"], "alice")
    row = _row("shop")
    assert row["status"] == "failed" and "INDEX_MAX_SYMBOLS" in row["error"]
    assert world["paid"] == [] and row["symbols"] == 0


@needs_neo4j
def test_a_run_that_failed_while_summarizing_is_not_up_to_date(world, monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: None)  # every summary fails
    repo_jobs.add(world["url"], "alice")
    row = _row("shop")
    assert row["status"] == "ready" and "3 summaries failed" in row["warning"]
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"does {s.name}")
    repo_jobs.refresh("shop", "alice")  # same commit, but summaries are missing
    row = _row("shop")
    assert row["stage"] == "indexed" and row["warning"] is None


@needs_neo4j
def test_a_restart_marks_unfinished_jobs_failed(world, monkeypatch):
    monkeypatch.setattr(repo_jobs, "_pool", type("P", (), {"submit": lambda s, *a: None})())
    repo_jobs.add(world["url"], "alice")
    repo_jobs.recover()
    row = _row("shop")
    assert row["status"] == "failed" and "restart" in row["error"]
    repo_jobs.refresh("shop", "alice")  # and it can be started again


@needs_neo4j
def test_progress_is_reported_while_indexing(world, monkeypatch):
    stages = []
    real = repo_jobs._set
    monkeypatch.setattr(repo_jobs, "_set",
                        lambda name, **p: stages.append(p.get("stage")) or real(name, **p))
    repo_jobs.add(world["url"], "alice")
    seen = [s for s in stages if s]
    order = ["starting", "cloning", "parsing", "summarizing", "grouping", "indexed"]
    assert [s for s in order if s in seen] == order
    assert [seen.index(s) for s in order] == sorted(seen.index(s) for s in order)


@needs_neo4j
def test_the_clone_keeps_no_token_and_no_symlinks(world, monkeypatch):
    os.symlink("/etc/passwd", world["remote"] / "link")
    _git(world["remote"], "add", "link")
    _git(world["remote"], "commit", "-qm", "symlink")
    monkeypatch.setenv("CLONE_TOKEN", "tok_supersecret")
    monkeypatch.setenv("CLONE_TOKEN_HOST", "")  # file:// has no host; the header is not sent
    repo_jobs.add(world["url"], "alice")
    clone = world["tmp"] / "clones" / "shop"
    assert not (clone / "link").is_symlink()  # checked out as a plain file
    config = (clone / ".git" / "config").read_text()
    assert "tok_supersecret" not in config and "extraHeader" not in config


def test_the_token_is_only_sent_to_its_host(monkeypatch):
    seen = []
    monkeypatch.setattr(repo_jobs.subprocess, "run",
                        lambda cmd, **kw: seen.append(kw["env"]) or
                        type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})())
    monkeypatch.setenv("CLONE_TOKEN", "tok_x")
    monkeypatch.setenv("CLONE_TOKEN_HOST", "github.com")
    repo_jobs._git(["clone", "x"], "https://github.com/a/b.git")
    repo_jobs._git(["clone", "x"], "https://gitlab.com/a/b.git")
    assert "GIT_CONFIG_VALUE_0" in seen[0] and "GIT_CONFIG_VALUE_0" not in seen[1]


# --- blast radius mode: analyse the change before the graph is updated ----------


V2 = V1.replace("def load(path):", "def load(path, strict):")


def _push(world, text, message):
    (world["remote"] / "core.py").write_text(text)
    _git(world["remote"], "commit", "-qam", message)


@needs_neo4j
def test_an_analysed_refresh_reports_the_change_then_updates_the_graph(world):
    repo_jobs.add(world["url"], "alice")
    first = _row("shop")["commit"]
    _push(world, V2, "signature change")

    repo_jobs.refresh("shop", "alice", analyze=True)
    row = _row("shop")
    assert row["commit"] == impact.git_head(str(world["remote"]))  # graph is current
    blast = row["blast"]
    assert blast["base"] == first and blast["head"] == row["commit"]
    assert blast["changed"] >= 1 and blast["risk"] in ("medium", "high", "critical")

    report = repo_jobs.pending_impact("shop")
    assert report.label.startswith("Refresh ")
    # computed against the OLD graph: run() still called the old load() then
    assert any(c.id == "core.load" and c.change == "signature_changed" for c in report.changed)
    assert any(a.id == "core.run" for a in report.affected)


@needs_neo4j
def test_a_refresh_in_another_mode_leaves_nothing_to_check(world):
    repo_jobs.add(world["url"], "alice")
    _push(world, V2, "one")
    repo_jobs.refresh("shop", "alice", analyze=True)
    assert repo_jobs.pending_impact("shop") is not None
    _push(world, V2 + "\n\ndef more():\n    return 2\n", "two")
    repo_jobs.refresh("shop", "alice")  # not analysed: the graph moves past the report
    assert repo_jobs.pending_impact("shop") is None
    assert _row("shop")["blast"] is None


@needs_neo4j
def test_an_analysed_refresh_with_no_new_commit_has_nothing_to_check(world):
    repo_jobs.add(world["url"], "alice")
    repo_jobs.refresh("shop", "alice", analyze=True)
    assert _row("shop")["stage"] == "up to date"
    assert repo_jobs.pending_impact("shop") is None


@needs_neo4j
def test_a_failed_analysis_leaves_the_graph_alone_so_it_can_be_retried(world, monkeypatch):
    repo_jobs.add(world["url"], "alice")
    before = _row("shop")["commit"]
    _push(world, V2, "change")

    def broken(*a, **k):
        raise RuntimeError("analysis broke")

    real = impact.analyze
    monkeypatch.setattr(impact, "analyze", broken)
    repo_jobs.refresh("shop", "alice", analyze=True)
    row = _row("shop")
    assert row["status"] == "failed" and "analysis broke" in row["error"]
    assert row["commit"] == before  # not re-indexed
    assert impact.git_head(str(world["tmp"] / "clones" / "shop")) == before  # not checked out

    monkeypatch.setattr(impact, "analyze", real)
    repo_jobs.refresh("shop", "alice", analyze=True)
    assert _row("shop")["blast"] is not None


@needs_neo4j
def test_analysis_runs_even_if_some_summaries_failed(world, monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: None)  # every summary fails
    repo_jobs.add(world["url"], "alice")
    _push(world, V2, "change")
    repo_jobs.refresh("shop", "alice", analyze=True)
    assert repo_jobs.pending_impact("shop") is not None


@needs_neo4j
def test_after_a_restore_the_existing_clone_is_reused_not_duplicated(world):
    """The database is restored from an older backup, or rebuilt, but the clone is
    still on disk. Adding the same URL again picks the clone back up."""
    repo_jobs.add(world["url"], "alice")
    graph.wipe_test_database()  # what a restore from before the repo was added looks like
    assert repo_jobs.add(world["url"], "alice") == "shop"
    assert _row("shop")["symbols"] == 3


@needs_neo4j
def test_a_clone_of_a_different_url_is_never_taken_over(world, tmp_path):
    other = tmp_path / "remote" / "other" / "shop.git"
    other.mkdir(parents=True)
    _git(other, "init", "-q", "-b", "main")
    (other / "x.py").write_text("def x():\n    return 1\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "x")
    repo_jobs.add(world["url"], "alice")
    graph.wipe_test_database()
    assert repo_jobs.add(f"file://{other}", "alice") == "acme-shop"
