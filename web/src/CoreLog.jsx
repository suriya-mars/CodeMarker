import React from "react";

// Order the traced symbols as core samples. Symbols joined by real edges form one
// sample, and depth is hops from that sample's shallowest symbol (a file above a class
// above a method). A symbol with no route to the others is its own sample and says so,
// rather than being bridged by anything invented.
const SHALLOW = { page: 0, file: 0, class: 1, interface: 1, function: 2, method: 2 };

// How the path reached a symbol, in words. Edges point from source to target.
const AS_SOURCE = { CONTAINS: "contains", CALLS: "calls", INHERITS: "extends", IMPORTS: "imports", HANDLES: "handles", TRIGGERS: "triggers", EXECUTES: "runs" };
const AS_TARGET = { CONTAINS: "in", CALLS: "called by", INHERITS: "extended by", IMPORTS: "imported by", HANDLES: "handled by", TRIGGERS: "triggered by", EXECUTES: "run by" };

export function buildCore(cited, path, describe) {
  const ids = [...new Set([...(cited || []), ...(path?.nodes || []).map((n) => n.id)])];
  const links = new Map(ids.map((id) => [id, []]));
  (path?.edges || []).forEach((e) => {
    links.get(e.source)?.push({ to: e.target, word: AS_SOURCE[e.type] });
    links.get(e.target)?.push({ to: e.source, word: AS_TARGET[e.type] });
  });

  const seen = new Set();
  const groups = [];
  ids.forEach((start) => {
    if (seen.has(start)) return;
    const group = [];
    const queue = [start];
    seen.add(start);
    while (queue.length) {
      const id = queue.shift();
      group.push(id);
      links.get(id).forEach((l) => {
        if (!seen.has(l.to)) {
          seen.add(l.to);
          queue.push(l.to);
        }
      });
    }
    groups.push(group);
  });
  groups.sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]));

  const rank = (id) => SHALLOW[describe(id).kind] ?? 3;
  const bands = [];
  groups.forEach((group, sample) => {
    const root = [...group].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))[0];
    const depth = new Map([[root, 0]]);
    const relation = new Map();
    const queue = [root];
    while (queue.length) {
      const id = queue.shift();
      links.get(id).forEach((l) => {
        if (!depth.has(l.to)) {
          depth.set(l.to, depth.get(id) + 1);
          // l.word is how `id` relates to l.to, so read it from the other side
          const back = links.get(l.to).find((x) => x.to === id);
          relation.set(l.to, `${back.word} ${describe(id).name}`);
          queue.push(l.to);
        }
      });
    }
    group
      .map((id) => ({
        id,
        ...describe(id),
        sample,
        alone: group.length === 1 && ids.length > 1,
        cited: (cited || []).includes(id),
        depth: depth.get(id),
        relation: relation.get(id) || null,
      }))
      .sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id))
      .forEach((band) => bands.push(band));
  });
  return bands;
}

function Summary({ text }) {
  if (!text) return null;
  const flagged = text.startsWith("[flagged");
  const body = flagged ? text.replace(/^\[flagged[^\]]*\]\s*/, "") : text;
  return (
    <p className="summary">
      {flagged && <span className="flag" title={text}>Flagged</span>}
      {body.split(/`([^`]+)`/).map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part))}
    </p>
  );
}

// Classes and functions have code to open; files and pages do not.
export const HAS_CODE = ["class", "interface", "function", "method"];

export function SymbolDetail({ symbol, onClose, onOpenCode }) {
  return (
    <section className="detail" aria-label="Selected symbol">
      <div className="detail-head">
        <span className={`kind-tag k-${symbol.kind}`}>{symbol.kind}</span>
        <button type="button" className="link" onClick={onClose}>Close</button>
      </div>
      <code className="full-id">{symbol.id}</code>
      {symbol.path && <small className="where">{symbol.path}</small>}
      <Summary text={symbol.summary} />
      {HAS_CODE.includes(symbol.kind) && (
        <button type="button" className="control open-code" onClick={onOpenCode}>
          Open code
        </button>
      )}
    </section>
  );
}

function Spend({ spend }) {
  if (!spend) return null;
  const tokens = (spend.usage?.input_tokens || 0) + (spend.usage?.output_tokens || 0);
  return (
    <dl className="spend">
      <div><dt>Tokens</dt><dd>{tokens.toLocaleString()}</dd></div>
      <div><dt>Est. cost</dt><dd>${(spend.usage?.cost_usd || 0).toFixed(4)}</dd></div>
      <div><dt>Time</dt><dd>{spend.seconds} s</dd></div>
    </dl>
  );
}

// The repo itself as a core, a band per symbol kind, thickness in proportion to the
// count. While a question is being traced the drill runs down it; how long the trace
// will take is unknown, so it never pretends to show progress.
function RepoCore({ counts, repo, busy, elapsed }) {
  const kinds = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const total = kinds.reduce((sum, [, n]) => sum + n, 0);
  if (!total) return null;
  return (
    <>
      <div className="log-head">
        <h2>Repository core</h2>
        {busy ? (
          <small role="status">Tracing, {elapsed} s</small>
        ) : (
          <small>{repo}: {total.toLocaleString()} symbols</small>
        )}
      </div>
      <ol className={`repo-core${busy ? " running" : ""}`}>
        {kinds.map(([kind, n]) => (
          <li key={kind} className={`hatch-${kind}`} style={{ flexGrow: n }}>
            <span>{kind}</span>
            <b>{n.toLocaleString()}</b>
          </li>
        ))}
      </ol>
      {!busy && (
        <p className="hint">
          Ask a question and the symbols it traces stack here by depth, with the path lit in the graph.
        </p>
      )}
    </>
  );
}

export default function CoreLog({ core, spend, busy, elapsed, counts, repo, selectedId, onPick, onWholeRepo }) {
  const samples = core ? new Set(core.map((b) => b.sample)).size : 0;
  const levels = core && core.length ? Math.max(...core.map((b) => b.depth)) + 1 : 0;
  const showRepo = busy || !core;
  return (
    <aside className="log" aria-label="Core log" aria-busy={busy}>
      {showRepo && <RepoCore counts={counts} repo={repo} busy={busy} elapsed={elapsed} />}
      {!showRepo && (
        <div className="log-head">
          <h2>Core log</h2>
          <button type="button" className="link" onClick={onWholeRepo}>Whole repo</button>
        </div>
      )}
      {!showRepo && core.length === 0 && (
        <p className="hint">
          This answer did not name a symbol the graph can match, so nothing is lit.
          Ask again and name the function or class you mean.
        </p>
      )}
      {!showRepo && core.length > 0 && (
        <>
          <p className="log-facts">
            {core.length} {core.length === 1 ? "symbol" : "symbols"}, {levels} {levels === 1 ? "level" : "levels"}
            {samples > 1 ? `, ${samples} separate samples` : ""}
          </p>
          <ol className="core" key={core.map((b) => b.id).join()}>
            {core.map((b, i) => (
              <li
                key={b.id}
                className={[
                  core[i + 1]?.sample === b.sample ? "drilled" : "",
                  i > 0 && core[i - 1].sample !== b.sample ? "new-sample" : "",
                ].join(" ")}
                style={{ "--i": i }}
              >
                <button
                  type="button"
                  className={`band hatch-${b.kind}${selectedId === b.id ? " on" : ""}`}
                  title={b.id}
                  onClick={() => onPick(b.id)}
                >
                  <span className="depth">{b.depth}</span>
                  <span className="band-body">
                    <span className="band-title">
                      <code>{b.name || b.id}</code>
                      <small>{b.kind}</small>
                    </span>
                    {b.relation && <small className="relation">{b.relation}</small>}
                    {!b.cited && <small className="relation">On the route, not cited</small>}
                    {b.alone && <small className="relation">No route to the others within 3 hops</small>}
                    <Summary text={b.summary} />
                  </span>
                </button>
              </li>
            ))}
          </ol>
          <p className="provenance">Levels and routes come from the parsed code. One-line summaries are written by the LLM.</p>
          <Spend spend={spend} />
        </>
      )}
    </aside>
  );
}
