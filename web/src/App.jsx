import React, { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchGraph, fetchRepos, fetchSource, ask } from "./api.js";
import { applyTheme, loadTheme, readTokens } from "./themes.js";
import ThemeMenu from "./ThemeMenu.jsx";
import CoreLog, { HAS_CODE, SymbolDetail, buildCore } from "./CoreLog.jsx";
import CodeView from "./CodeView.jsx";

cytoscape.use(fcose);

// Answers quote repository text, so an answer is untrusted content. Raw HTML stays
// inert (react-markdown does not render it), images are dropped so nothing can fetch
// a URL just by being displayed, and links become plain text showing where they
// point, so injected text cannot carry data out or send a reader somewhere.
const MARKDOWN_PLUGINS = [remarkGfm];
const NO_IMAGES = ["img"];
const MARKDOWN_COMPONENTS = {
  a({ children, href }) {
    const text = String(children);
    return <span>{text}{href && href !== text ? ` (${href})` : ""}</span>;
  },
};

function buildElements(graph) {
  const ids = new Set(graph.nodes.map((n) => n.id));
  const nodes = graph.nodes.map((n) => ({
    data: {
      id: n.id,
      label: n.name || n.id,
      kind: n.kind,
      path: n.path || "",
      summary: n.summary || "",
      degree: n.degree || 0,
    },
  }));
  const edges = graph.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e, i) => ({
      data: { id: `e${i}`, source: e.source, target: e.target, type: e.type },
    }));
  return [...nodes, ...edges];
}

const narrow = () => window.matchMedia("(max-width: 760px)").matches;

function graphStyle() {
  const t = readTokens();
  return [
    {
      selector: "node",
      style: {
        "background-color": (n) => t.kinds[n.data("kind")] || t.text,
        width: (n) => 12 + Math.min(28, n.data("degree") * 3),
        height: (n) => 12 + Math.min(28, n.data("degree") * 3),
        label: "data(label)",
        // minor symbols label only once zoomed in, which ends the overview's label soup
        "font-size": (n) => (n.data("degree") >= 3 || n.data("kind") === "class" ? 11 : 6),
        "font-family": "Hanken Grotesk Variable, system-ui, sans-serif",
        color: t.text,
        "text-valign": "bottom",
        "text-margin-y": 3,
        "min-zoomed-font-size": 9,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1,
        "line-color": t.edge,
        "target-arrow-color": t.edge,
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        "arrow-scale": 0.6,
      },
    },
    {
      // Structure, not dependency: keeps a function beside its file without
      // competing with the calls that the graph is read for.
      selector: 'edge[type = "CONTAINS"]',
      style: { width: 0.5, "line-color": t.contains, "target-arrow-shape": "none" },
    },
    { selector: ".dim", style: { opacity: 0.14 } },
    {
      // a label you check against the log: readable over any edge, with a halo rather
      // than a solid box, which would cut the lit path where it crosses
      selector: "node.via, node.lit",
      style: {
        opacity: 1,
        "z-index": 10,
        color: t.body,
        "min-zoomed-font-size": 0,
        "text-outline-color": t.bg,
        "text-outline-width": 2.5,
      },
    },
    { selector: "node.via", style: { "border-width": 2, "border-color": t.via } },
    { selector: "node.lit", style: { "border-width": 3, "border-color": t.live } },
    {
      selector: "edge.lit",
      style: { "line-color": t.live, "target-arrow-color": t.live, width: 3, opacity: 1, "z-index": 5 },
    },
    { selector: "node.picked", style: { "overlay-color": t.live, "overlay-opacity": 0.22, "overlay-padding": 6 } },
  ];
}

export default function App() {
  const cyEl = useRef(null);
  const cyRef = useRef(null);
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [repos, setRepos] = useState([]);
  const [repo, setRepo] = useState(null);
  const [status, setStatus] = useState("Loading repositories…");
  const [theme, setTheme] = useState(loadTheme);
  const [stats, setStats] = useState({ symbols: 0, edges: 0, counts: {} });
  const [active, setActive] = useState(null); // the answer currently shown in the core log
  const [selected, setSelected] = useState(null);
  const [code, setCode] = useState(null); // { loading } | { error } | { source... }
  const [codeOpen, setCodeOpen] = useState(false);
  const endRef = useRef(null);
  const graphEl = useRef(null);

  useEffect(() => {
    applyTheme(theme);
    cyRef.current?.style(graphStyle());
  }, [theme]);

  useEffect(() => {
    fetchRepos()
      .then((list) => {
        setRepos(list);
        if (list.length > 0) setRepo(list[0].repo);
        else setStatus("No repositories indexed yet. Run the indexer, then reload this page.");
      })
      .catch((e) => setStatus(`${e.message}. Check the API is running, then reload.`));
  }, []);

  useEffect(() => {
    if (!repo) return undefined;
    let cancelled = false;
    setStatus(`Loading ${repo}…`);
    fetchGraph(repo)
      .then((graph) => {
        if (cancelled || !cyEl.current) return;
        const counts = {};
        graph.nodes.forEach((n) => {
          counts[n.kind] = (counts[n.kind] || 0) + 1;
        });
        setStats({ symbols: graph.nodes.length, edges: graph.edges.length, counts });
        setStatus(graph.nodes.length === 0 ? `${repo} has no symbols to show.` : "");
        const cy = cytoscape({
          container: cyEl.current,
          elements: buildElements(graph),
          style: graphStyle(),
          maxZoom: 3,
          layout: {
            name: "fcose",
            animate: false,
            quality: "default",
            nodeSeparation: 80,
            idealEdgeLength: (edge) => (edge.data("type") === "CONTAINS" ? 45 : 90),
          },
        });
        cy.on("zoom", sizeLitLabels);
        cy.on("tap", "node", (evt) => pick(evt.target.id()));
        cy.on("tap", (evt) => {
          if (evt.target === cy) clearPick();
        });
        cyRef.current = cy;
      })
      .catch((e) => setStatus(`${e.message}. Reload to try again.`));
    return () => {
      cancelled = true;
      cyRef.current?.destroy();
      cyRef.current = null;
    };
  }, [repo]);

  useEffect(() => {
    if (!busy) return undefined;
    const started = Date.now();
    setElapsed(0);
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [messages]);

  // While the viewer is open it follows the selection; a symbol with no code closes it.
  useEffect(() => {
    if (!codeOpen) return undefined;
    if (!selected || !HAS_CODE.includes(selected.kind)) {
      setCodeOpen(false);
      return undefined;
    }
    let cancelled = false;
    setCode({ loading: true, id: selected.id });
    fetchSource(selected.id, repo)
      .then((data) => {
        if (!cancelled) setCode(data);
      })
      .catch((e) => {
        if (!cancelled) setCode({ error: e.message, id: selected.id });
      });
    return () => {
      cancelled = true;
    };
  }, [codeOpen, selected?.id, repo]); // eslint-disable-line react-hooks/exhaustive-deps

  function describe(id) {
    const n = cyRef.current?.getElementById(id);
    if (!n || n.empty()) return { name: id, kind: "file", summary: "", path: "" };
    return { name: n.data("label"), kind: n.data("kind"), summary: n.data("summary"), path: n.data("path") };
  }

  // Lit labels stay about 12px on screen at any zoom.
  function sizeLitLabels() {
    const cy = cyRef.current;
    cy?.nodes(".lit, .via").style("font-size", 12 / cy.zoom());
  }

  function clearPick() {
    cyRef.current?.nodes().removeClass("picked");
    setSelected(null);
  }

  function pick(id) {
    const cy = cyRef.current;
    if (!cy) return;
    const n = cy.getElementById(id);
    cy.nodes().removeClass("picked");
    if (n.nonempty()) {
      n.addClass("picked");
      // on a phone, close in on the symbol and its lit neighbours so labels do not collide
      if (narrow()) {
        cy.animate({ fit: { eles: n.closedNeighborhood(".lit, .via"), padding: 60 }, duration: 200 });
      } else {
        cy.animate({ center: { eles: n }, duration: 200 });
      }
    }
    setSelected({ id, ...describe(id) });
    // stacked layout: the graph may be off screen when a band is tapped
    if (narrow()) graphEl.current?.scrollIntoView({ block: "start" });
  }

  // Cited symbols are lit; `path` (from the server, real graph edges) joins them,
  // including symbols in between that were not cited. Anything the overview left out
  // to stay readable is added now, so the path is never drawn with a gap.
  function highlightPath(ids, path) {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes(".lit, .via").removeStyle("font-size");
    cy.elements().removeClass("lit via").addClass("dim");
    if (!ids || ids.length === 0) {
      cy.elements().removeClass("dim");
      return;
    }
    const lit = cy.collection();
    const via = cy.collection();
    (path?.nodes || []).forEach((n) => {
      if (cy.getElementById(n.id).empty()) {
        const near = cy.getElementById(ids[0]).position();
        cy.add({
          data: { id: n.id, label: n.name || n.id, kind: n.kind, summary: "", degree: 0 },
          position: near ? { x: near.x + 40, y: near.y + 40 } : undefined,
        });
      }
      if (!ids.includes(n.id)) via.merge(cy.getElementById(n.id));
    });
    ids.forEach((id) => {
      const n = cy.getElementById(id);
      if (n.nonempty()) lit.merge(n);
    });
    const edges = cy.collection();
    (path?.edges || []).forEach((e) => {
      const found = cy
        .getElementById(e.source)
        .edgesTo(cy.getElementById(e.target))
        .filter((x) => x.data("type") === e.type);
      edges.merge(
        found.nonempty()
          ? found
          : cy.add({
              data: { id: `p-${e.type}-${e.source}-${e.target}`, source: e.source, target: e.target, type: e.type },
            })
      );
    });
    // direct edges between cited symbols, for the no-path case
    lit.forEach((n) => {
      n.connectedEdges().forEach((e) => {
        if (lit.contains(e.source()) && lit.contains(e.target())) edges.merge(e);
      });
    });
    lit.removeClass("dim").addClass("lit");
    edges.removeClass("dim").addClass("lit");
    via.removeClass("dim").addClass("via");
    const shown = lit.union(via);
    sizeLitLabels();
    if (shown.nonempty()) {
      // a phone cannot hold separate samples apart at a readable size: frame the largest
      const frame = narrow()
        ? shown.union(edges).components().sort((x, y) => y.length - x.length)[0]
        : shown;
      cy.animate({ fit: { eles: frame, padding: narrow() ? 60 : 80 }, duration: 250 });
    }
  }

  function showWholeRepo() {
    const cy = cyRef.current;
    setActive(null);
    clearPick();
    if (!cy) return;
    highlightPath([]);
    cy.animate({ fit: { padding: 30 }, duration: 250 });
  }

  function showAnswer(m) {
    setActive(m);
    clearPick();
    highlightPath(m.cited, m.path);
  }

  function onRepoChange(e) {
    setMessages([]); // earlier answers describe the previous repo
    setActive(null);
    setSelected(null);
    setRepo(e.target.value);
  }

  async function onSubmit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy || !repo) return;
    setMessages((m) => [...m, { role: "user", text: q }]);
    setQuestion("");
    setBusy(true);
    setSelected(null);
    const started = Date.now();
    try {
      const res = await ask(q, repo);
      const answer = {
        role: "assistant",
        text: res.answer,
        cited: res.cited_ids || [],
        path: res.path,
        spend: { usage: res.usage, seconds: Math.round((Date.now() - started) / 1000) },
      };
      setMessages((m) => [...m, answer]);
      showAnswer(answer);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", text: err.message }]);
    } finally {
      setBusy(false);
    }
  }

  const core = useMemo(
    () => (active ? buildCore(active.cited, active.path, describe) : null),
    [active] // eslint-disable-line react-hooks/exhaustive-deps
  );

  return (
    <div className="app">
      <main className="graph" ref={graphEl}>
        <div id="cy" ref={cyEl} />
        {status && <div className="graph-status" role="status">{status}</div>}
        {codeOpen && code && <CodeView code={code} onClose={() => setCodeOpen(false)} />}
        {stats.symbols > 0 && (
          <div className="graph-head">
            <span className="stat">
              {stats.symbols.toLocaleString()} symbols, {stats.edges.toLocaleString()} edges
            </span>
            <span className="legend">
              {Object.keys(stats.counts).map((kind) => (
                <span key={kind}><i className={`k-${kind}`} />{kind}</span>
              ))}
            </span>
          </div>
        )}
      </main>
      <div className="log-column">
        {selected && <SymbolDetail symbol={selected} onClose={clearPick} onOpenCode={() => setCodeOpen(true)} />}
        <CoreLog
          core={core}
          spend={active?.spend}
          busy={busy}
          elapsed={elapsed}
          counts={stats.counts}
          repo={repo}
          selectedId={selected?.id}
          onPick={pick}
          onWholeRepo={showWholeRepo}
        />
      </div>
      <section className="panel" aria-label="Questions">
        <header className="panel-head">
          <h1>RepoDigger</h1>
          <div className="panel-controls">
            {repos.length > 1 && (
              <select className="control" aria-label="Repository" value={repo || ""} onChange={onRepoChange} disabled={busy}>
                {repos.map((r) => (
                  <option key={r.repo} value={r.repo}>{r.repo}</option>
                ))}
              </select>
            )}
            {repos.length === 1 && <span className="repo-name">{repos[0].repo}</span>}
            <ThemeMenu theme={theme} onChange={setTheme} />
          </div>
        </header>
        <div className="messages">
          {messages.length === 0 && (
            <p className="intro">
              Ask how something in {repo || "the codebase"} works, for example “how does
              authentication work?”. The answer is traced through the graph, so you can check it.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.role === "assistant" ? (
                <>
                  <div className="md">
                    <Markdown
                      remarkPlugins={MARKDOWN_PLUGINS}
                      disallowedElements={NO_IMAGES}
                      components={MARKDOWN_COMPONENTS}
                    >
                      {m.text}
                    </Markdown>
                  </div>
                  {m !== active && m.cited.length > 0 && (
                    <button type="button" className="link" onClick={() => showAnswer(m)}>
                      Show this path
                    </button>
                  )}
                </>
              ) : (
                m.text
              )}
            </div>
          ))}
          <div ref={endRef} />
        </div>
        <form onSubmit={onSubmit}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={repo ? `Ask about ${repo}…` : "Index a repository first"}
            aria-label="Your question"
            disabled={busy || !repo}
          />
          <button type="submit" className="ask" disabled={busy || !repo || !question.trim()}>
            {busy ? "Asking…" : "Ask"}
          </button>
        </form>
      </section>
    </div>
  );
}
