import React, { useState } from "react";
import { addRepo, deleteRepo, refreshRepo } from "./api.js";

const ACTIVE = ["queued", "running"];

function ago(seconds) {
  if (!seconds) return "";
  const s = Math.max(0, Date.now() / 1000 - seconds);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { dateStyle: "medium" });
}

const shortSha = (sha) => (sha || "").slice(0, 7);

// What a repo is doing, in words. Counts are only shown where they are known.
function state(r) {
  // one index runs at a time, server-wide
  if (r.status === "queued") return "Queued. Starts when the index before it finishes.";
  if (r.status === "running") {
    if (r.stage === "cloning") return "Cloning…";
    if (r.stage === "fetching") return "Checking for new commits…";
    if (r.stage === "analyzing") return "Analysing the change before updating the graph…";
    if (r.stage === "parsing") return "Reading the code…";
    if (r.stage === "summarizing") {
      return r.total ? `Summarizing ${r.done.toLocaleString()} of ${r.total.toLocaleString()}` : "Summarizing…";
    }
    if (r.stage === "grouping") return "Grouping related code…";
    return "Starting…";
  }
  if (r.status === "failed") return null;
  const at = r.commit ? `at ${shortSha(r.commit)}` : "";
  if (r.stage === "up to date") return `Up to date ${at}, checked ${ago(r.last_checked)}`;
  const skipped = r.skipped_files
    ? `${r.skipped_files.toLocaleString()} generated ${r.skipped_files === 1 ? "file" : "files"} skipped`
    : "";
  return [`${r.symbols.toLocaleString()} symbols`, skipped, at, ago(r.indexed_at)].filter(Boolean).join(", ");
}

// Deleting cannot be undone, so the name must be typed again, here and on the server.
function ConfirmDelete({ repo, onDone, onCancel }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function onSubmit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await deleteRepo(repo.repo);
      onDone(repo.repo);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <form className="confirm-delete" onSubmit={onSubmit}>
      <p>
        Delete <b>{repo.repo}</b>? This removes its graph, its impact reports and every
        conversation about it{repo.url ? ", and its clone" : ""}, for everyone. It cannot be undone.
        {!repo.url && " Its files in the repos folder are kept."}
      </p>
      <label className="field">
        Type {repo.repo} to confirm
        <input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus spellCheck="false"
          autoComplete="off" disabled={busy} />
      </label>
      <div className="confirm-actions">
        <button type="submit" className="danger-button" disabled={busy || typed !== repo.repo}>
          {busy ? "Deleting…" : "Delete"}
        </button>
        <button type="button" className="link" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
      {error && <small className="repo-error">{error}</small>}
    </form>
  );
}

export default function Repos({ repos, current, onOpen, onChanged, onDeleted, analyze }) {
  const [deleting, setDeleting] = useState(null);
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const mine = repos.find((r) => r.mine);

  async function run(action) {
    setError("");
    try {
      await action();
      onChanged();
    } catch (e) {
      setError(e.message);
    }
  }

  async function onAdd(e) {
    e.preventDefault();
    if (!url.trim()) return;
    setAdding(true);
    await run(async () => {
      await addRepo(url.trim());
      setUrl("");
    });
    setAdding(false);
  }

  return (
    <div className="messages">
      <form className="compare" onSubmit={onAdd}>
        <label className="field">
          Repository URL
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/name"
            spellCheck="false"
            autoComplete="off"
            disabled={adding}
          />
        </label>
        <div className="compare-run">
          <button type="submit" className="ask" disabled={adding || !url.trim() || Boolean(mine)}>
            {adding ? "Adding…" : "Add and index"}
          </button>
          <small>
            Indexing pays for a one-line LLM summary of every class and function. Refreshing pays
            only for code that changed.
          </small>
        </div>
      </form>
      {mine && (
        <p className="hint" role="status">
          You are indexing {mine.repo}. You can add or refresh another repository when it finishes.
        </p>
      )}
      {error && <p className="msg error" role="alert">{error}</p>}

      <section aria-label="Repositories">
        <h2 className="report-heading">Repositories</h2>
        {analyze && (
          <p className="hint mode-note">
            Blast radius mode is on, so Refresh analyses each new change before updating the graph.
          </p>
        )}
        {repos.length === 0 && <p className="hint">No repositories yet. Add one by its URL above.</p>}
        <ul className="report-list" aria-live="polite">
          {repos.map((r) => {
            const active = ACTIVE.includes(r.status);
            const words = state(r);
            return (
              <li key={r.repo} className={`repo-row${r.repo === current ? " on" : ""}`}>
                <div className="repo-main">
                  <button
                    type="button"
                    className="repo-open"
                    onClick={() => onOpen(r.repo)}
                    disabled={r.symbols === 0}
                    title={r.symbols === 0 ? "Not indexed yet" : `Open ${r.repo}`}
                  >
                    {r.repo}
                  </button>
                  {r.url && <small className="repo-url">{r.url.replace(/^https:\/\//, "").replace(/\.git$/, "")}</small>}
                  {words && <small className={active ? "repo-state active" : "repo-state"}>{words}</small>}
                  {r.status === "failed" && <small className="repo-error">{r.error || "Indexing failed."}</small>}
                  {r.status === "ready" && r.warning && <small className="repo-warning">{r.warning}</small>}
                  {r.blast && <small className="repo-state">Blast radius ready for the latest change</small>}
                </div>
                <div className="repo-actions">
                <button
                  type="button"
                  className="control"
                  onClick={() => run(() => refreshRepo(r.repo, analyze))}
                  disabled={active || Boolean(mine)}
                  aria-label={analyze ? `Refresh and analyse ${r.repo}` : `Refresh ${r.repo}`}
                >
                  {active ? "Working…" : analyze ? "Refresh and analyse" : "Refresh"}
                </button>
                <button
                  type="button"
                  className="link danger-link"
                  onClick={() => setDeleting(r.repo)}
                  disabled={active}
                  aria-label={`Delete ${r.repo}`}
                >
                  Delete
                </button>
                </div>
                {deleting === r.repo && (
                  <ConfirmDelete
                    repo={r}
                    onCancel={() => setDeleting(null)}
                    onDone={(name) => {
                      setDeleting(null);
                      onDeleted?.(name);
                      onChanged();
                    }}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
