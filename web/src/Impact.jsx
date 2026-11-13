import React, { useEffect, useRef, useState } from "react";
import Answer from "./Answer.jsx";
import { Spend, Summary } from "./CoreLog.jsx";
import { askReport, fetchImpactRange, fetchReports, runImpact } from "./api.js";

const BANDS_AT_ONCE = 100;
const CHANGE_WORDS = {
  added: "Added",
  removed: "Removed",
  modified: "Modified",
  signature_changed: "Signature changed",
};
const FILE_WORDS = { A: "Added", M: "Modified", D: "Deleted", R: "Renamed", C: "Copied", T: "Type changed" };
const EDGE_WORDS = {
  CALLS: "calls",
  HANDLES: "handles",
  TRIGGERS: "triggers",
  EXECUTES: "runs",
  INHERITS: "extends",
  IMPLEMENTS: "same contract as",
};

const shortSha = (sha) => (sha || "").slice(0, 10);
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
const lastPart = (id) => id.split(/[.:/]/).filter(Boolean).pop() || id;
const when = (seconds) =>
  new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function RiskLevel({ level }) {
  return <span className={`risk risk-${level}`}>{level} risk</span>;
}

// What the graph lights for a report: changed symbols, and the real route from each
// affected symbol back to the change. Added symbols are not in the graph yet.
export function reportPath(report) {
  const nodes = new Map();
  const edges = [];
  const add = (id, name, kind) => {
    if (!nodes.has(id)) nodes.set(id, { id, name: name || lastPart(id), kind: kind || "function" });
  };
  const changed = report.changed.filter((c) => c.change !== "added");
  changed.forEach((c) => add(c.id, c.name, c.kind));
  report.affected.forEach((a) => {
    add(a.id, a.name, a.kind);
    const [from, to] = a.path_to_change;
    if (to) {
      add(to);
      edges.push({ source: from, target: to, type: a.edge });
    }
  });
  return { ids: changed.map((c) => c.id), path: { nodes: [...nodes.values()], edges } };
}

// One sample per changed symbol, with what depends on it below: the drill line joins
// bands of one sample only, so it never suggests a route that does not exist. A symbol
// reached from two changes appears under the nearer one, as the report records it.
function bandsOf(report) {
  const names = new Map([...report.changed, ...report.affected].map((s) => [s.id, s.name]));
  const byChange = new Map();
  report.affected.forEach((a) => {
    if (!byChange.has(a.via)) byChange.set(a.via, []);
    byChange.get(a.via).push(a);
  });
  return report.changed.flatMap((c, sample) => [
    {
      id: c.id,
      name: c.name,
      kind: c.kind,
      path: c.path,
      summary: c.summary,
      depth: 0,
      sample,
      relation: CHANGE_WORDS[c.change],
      indexed: c.change !== "added",
    },
    ...(byChange.get(c.id) || []).map((a) => {
      const parent = a.path_to_change[1];
      const tags = [a.is_entry_point && "Entry point", a.is_test && "Test"].filter(Boolean);
      return {
        id: a.id,
        name: a.name,
        kind: a.kind,
        path: a.path,
        summary: a.summary,
        depth: a.distance,
        sample,
        relation: `${EDGE_WORDS[a.edge] || a.edge} ${names.get(parent) || lastPart(parent)}`,
        tags,
        indexed: true,
      };
    }),
  ]);
}

export function ImpactLog({ report, selectedId, onPick, onClose }) {
  const [everything, setEverything] = useState(false);
  const bands = bandsOf(report);
  const shown = everything ? bands : bands.slice(0, BANDS_AT_ONCE);
  return (
    <aside className="log" aria-label="Blast radius">
      <div className="log-head">
        <h2>Blast radius</h2>
        <button type="button" className="link" onClick={onClose}>Whole repo</button>
      </div>
      {bands.length === 0 ? (
        <p className="hint">This change touches no symbols in a supported language.</p>
      ) : (
        <>
          <p className="log-facts">
            {plural(report.changed.length, "changed symbol")}, {plural(report.affected.length, "affected symbol")}.
            Each number is the hops from the change above it.
          </p>
          <ol className="core">
            {shown.map((b, i) => (
              <li
                key={b.id}
                className={[
                  shown[i + 1]?.sample === b.sample ? "drilled" : "",
                  i > 0 && shown[i - 1].sample !== b.sample ? "new-sample" : "",
                ].join(" ")}
                style={{ "--i": Math.min(i, 12) }}
              >
                <button
                  type="button"
                  className={`band hatch-${b.kind}${selectedId === b.id ? " on" : ""}`}
                  title={b.id}
                  onClick={() => onPick(b.id, b)}
                >
                  <span className="depth">{b.depth}</span>
                  <span className="band-body">
                    <span className="band-title">
                      <code>{b.name || b.id}</code>
                      <small>{b.kind}</small>
                    </span>
                    <small className="relation">
                      {b.relation}
                      {b.tags?.length > 0 ? `. ${b.tags.join(", ")}` : ""}
                    </small>
                    <Summary text={b.summary} />
                  </span>
                </button>
              </li>
            ))}
          </ol>
          {bands.length > shown.length && (
            <button type="button" className="control show-all" onClick={() => setEverything(true)}>
              Show all {bands.length.toLocaleString()}
            </button>
          )}
          <p className="provenance">
            Hops and routes come from the parsed code. Calls the parser cannot resolve are dropped, so
            this can under-report. Summaries and Open code show the code as it was indexed.
          </p>
        </>
      )}
    </aside>
  );
}

function Compare({ repo, onReport }) {
  const [range, setRange] = useState(null);
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setRange(null);
    fetchImpactRange(repo)
      .then((r) => {
        if (!cancelled) setRange(r);
      })
      .catch((e) => {
        if (!cancelled) setRange({ failed: e.message });
      });
    return () => {
      cancelled = true;
    };
  }, [repo]);

  async function onSubmit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      onReport(await runImpact({ repo, base: base.trim() || null, head: head.trim() || "HEAD" }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const upToDate = range?.ahead === 0 && !base.trim() && !head.trim();
  return (
    <form className="compare" onSubmit={onSubmit}>
      {range?.failed && <p className="compare-state" role="alert">{range.failed}</p>}
      {range && !range.failed && !range.git && (
        <p className="compare-state">
          {repo} is not a git repository, so there are no commits to compare.
        </p>
      )}
      {range?.git && (
        <p className="compare-state">
          {!range.indexed && "No indexed commit is recorded for this repo. Index it again to get an exact report. "}
          {range.indexed && range.ahead === 0 && (
            <>The graph is at the latest commit, <code>{shortSha(range.head.sha)}</code>. Commit a change to see its impact, or enter a range below.</>
          )}
          {range.indexed && range.ahead > 0 && (
            <>The latest commit is {plural(range.ahead, "commit")} ahead of the graph.</>
          )}
        </p>
      )}
      <div className="compare-fields">
        <label>
          From
          <input value={base} onChange={(e) => setBase(e.target.value)} list="impact-commits"
            placeholder="Indexed commit" spellCheck="false" autoComplete="off" />
        </label>
        <label>
          To
          <input value={head} onChange={(e) => setHead(e.target.value)} list="impact-commits"
            placeholder="Latest commit" spellCheck="false" autoComplete="off" />
        </label>
      </div>
      <datalist id="impact-commits">
        {(range?.recent || []).map((c) => (
          <option key={c.sha} value={c.sha}>{c.subject}</option>
        ))}
      </datalist>
      <div className="compare-run">
        <button type="submit" className="ask" disabled={busy || upToDate || range?.git === false}>
          {busy ? "Analyzing…" : "Analyze"}
        </button>
        <small>Computed from the graph. No LLM call, no token cost.</small>
      </div>
      {error && <p className="msg error" role="alert">{error}</p>}
    </form>
  );
}

function ReportList({ repo, version, onOpen }) {
  const [reports, setReports] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchReports(repo)
      .then((list) => {
        if (!cancelled) setReports(list);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [repo, version]);

  if (error) return <p className="msg error" role="alert">{error}</p>;
  if (!reports) return <p className="hint" role="status">Loading saved reports…</p>;
  if (reports.length === 0) {
    return <p className="hint">Reports you run here, and reports from pull requests, are kept in this list.</p>;
  }
  return (
    <section aria-label="Saved reports">
      <h2 className="report-heading">Saved reports</h2>
      <ul className="report-list">
        {reports.map((r) => (
          <li key={r.id}>
            <button type="button" className="report-row" onClick={() => onOpen(r.id)}>
              <span className="report-row-title">
                <span className="report-row-name">
                  {r.label || <code>{shortSha(r.base)}..{shortSha(r.head)}</code>}
                </span>
                <RiskLevel level={r.risk} />
              </span>
              <small>{r.label ? <><code>{shortSha(r.head)}</code>, </> : null}{when(r.created_at)}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SymbolLink({ id, name, onPick }) {
  return (
    <button type="button" className="symbol-link" title={id} onClick={() => onPick(id, { name: name || lastPart(id) })}>
      {id}
    </button>
  );
}

function download(report, markdown) {
  const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `impact-${report.repo}-${report.id}.md`;
  link.click();
  URL.revokeObjectURL(url);
}

function ReportFacts({ report, markdown, onPick, onBack }) {
  const risk = report.risk;
  const diffs = report.changed.filter((c) => c.diff);
  return (
    <>
      <div className="report-nav">
        <button type="button" className="link" onClick={onBack}>All reports</button>
        <button type="button" className="link" onClick={() => download(report, markdown)}>Download Markdown</button>
      </div>
      <header className="report-head">
        <div className="report-title">
          <h2>{report.label || "Impact report"}</h2>
          <RiskLevel level={risk.level} />
        </div>
        <code className="report-range">{shortSha(report.base)}..{shortSha(report.head)}</code>
        <dl className="report-counts">
          <div><dt>Files</dt><dd>{report.files.length}</dd></div>
          <div><dt>Changed</dt><dd>{report.changed.length}</dd></div>
          <div><dt>Affected</dt><dd>{report.affected.length}{report.truncated ? "+" : ""}</dd></div>
          <div><dt>Tests</dt><dd>{risk.affected_tests}</dd></div>
          <div><dt>Pages</dt><dd>{risk.affected_pages}</dd></div>
          <div><dt>Areas</dt><dd>{report.communities.length}</dd></div>
        </dl>
      </header>

      {report.warnings.map((w) => <p key={w} className="report-warning">{w}</p>)}

      {risk.flags.length > 0 && (
        <section aria-label="Risk flags">
          <h3>Risk flags</h3>
          <ul className="flag-list">
            {risk.flags.map((f, i) => (
              <li key={`${f.code}-${f.symbol || i}`}>
                <RiskLevel level={f.level} />
                <p>{f.message}</p>
                {f.dependents?.length > 0 && (
                  <ul className="symbol-list">
                    {f.dependents.map((d) => <li key={d}><SymbolLink id={d} onPick={onPick} /></li>)}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {diffs.length > 0 && (
        <section aria-label="Changes">
          <h3>What changed</h3>
          {diffs.map((c) => (
            <details key={c.id} className="report-more">
              <summary>
                <code>{c.name || c.id}</code> <small>{CHANGE_WORDS[c.change]}</small>
              </summary>
              {/* repository content: rendered as text, never as HTML */}
              <pre className="report-diff" tabIndex={0}>{c.diff}</pre>
            </details>
          ))}
        </section>
      )}

      <details className="report-more">
        <summary>Files <small>{report.files.length}</small></summary>
        <ul className="plain-list">
          {report.files.map((f) => (
            <li key={f.path}>
              <small>{FILE_WORDS[f.status] || f.status}</small> <code>{f.path}</code>
              {f.status === "R" && <small> from <code>{f.old_path}</code></small>}
            </li>
          ))}
        </ul>
      </details>

      {report.communities.length > 0 && (
        <details className="report-more">
          <summary>Architectural areas <small>{report.communities.length}</small></summary>
          <ul className="plain-list">
            {report.communities.map((c) => (
              <li key={c.id}>
                <small>
                  {plural(c.affected, "affected symbol")}{c.contains_change ? ", contains the change" : ""}
                </small>
                <Summary text={c.summary} />
              </li>
            ))}
          </ul>
        </details>
      )}

      {report.importers.length > 0 && (
        <details className="report-more">
          <summary>Files importing a changed file <small>{report.importers.length}</small></summary>
          <ul className="symbol-list">
            {report.importers.map((i) => <li key={i.id}><SymbolLink id={i.id} onPick={onPick} /></li>)}
          </ul>
        </details>
      )}
    </>
  );
}

function ReportAnswers({ messages, busy }) {
  if (messages.length === 0) return null;
  return (
    <section className="report-answers" aria-label="LLM answers about this report">
      <h3>LLM explanation</h3>
      {messages.map((m, i) => (
        <div key={i} className={`msg ${m.role}`}>
          {m.role === "assistant" ? (<><Answer text={m.text} /><Spend spend={m.spend} /></>) : m.text}
        </div>
      ))}
      {busy && <p className="hint" role="status">The LLM is reading the report…</p>}
    </section>
  );
}

// Facts on top, the LLM only on request, and the two never mixed.
function OpenedReport({ opened, onPick, onBack }) {
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef(null);

  useEffect(() => {
    if (messages.length > 0) end.current?.scrollIntoView({ block: "nearest" });
  }, [messages, busy]);

  async function onSubmit(e) {
    e.preventDefault();
    if (busy) return;
    const q = question.trim();
    const started = Date.now();
    setMessages((m) => [...m, { role: "user", text: q || "Explain this change for a reviewer." }]);
    setQuestion("");
    setBusy(true);
    try {
      const res = await askReport(opened.report.id, q || null);
      setMessages((m) => [...m, {
        role: "assistant",
        text: res.answer,
        spend: { usage: res.usage, seconds: Math.round((Date.now() - started) / 1000) },
      }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", text: err.message }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="messages report">
        <ReportFacts report={opened.report} markdown={opened.markdown} onPick={onPick} onBack={onBack} />
        <ReportAnswers messages={messages} busy={busy} />
        <div ref={end} />
      </div>
      <small className="dock-note">
        Everything above the explanation was computed without an LLM. Explaining calls the LLM and
        counts toward your daily tokens.
      </small>
      <form onSubmit={onSubmit}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about this change (optional)"
          aria-label="Question about this report"
          disabled={busy}
        />
        <button type="submit" className="ask" disabled={busy}>
          {busy ? "Asking…" : question.trim() ? "Ask" : "Explain"}
        </button>
      </form>
    </>
  );
}

export function ReviewPanel({ repo, opened, error, onOpen, onShow, onBack, onPick }) {
  const [version, setVersion] = useState(0);

  if (opened) {
    return <OpenedReport key={opened.report.id} opened={opened} onPick={onPick} onBack={onBack} />;
  }
  return (
    <div className="messages">
      {!repo && <p className="intro">Index a repository first.</p>}
      {repo && (
        <>
          <p className="intro">
            See what a commit affects: the symbols it changes, everything upstream of them, and what
            is likely to break.
          </p>
          <Compare repo={repo} onReport={(res) => { setVersion((v) => v + 1); onShow(res); }} />
          {error && <p className="msg error" role="alert">{error}</p>}
          <ReportList repo={repo} version={version} onOpen={onOpen} />
        </>
      )}
    </div>
  );
}
