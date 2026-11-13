import React, { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import { fetchGraph, fetchReport, fetchRepos, fetchSource, ask, refreshRepo } from "./api.js";
import { applyTheme, loadTheme, readTokens } from "./themes.js";
import ThemeMenu from "./ThemeMenu.jsx";
import CoreLog, { HAS_CODE, SymbolDetail, buildCore } from "./CoreLog.jsx";
import CodeView from "./CodeView.jsx";
import Answer from "./Answer.jsx";
import { ImpactLog, ReviewPanel, reportPath } from "./Impact.jsx";
import Repos from "./Repos.jsx";
import { BlastStatus, ModePicker, modeById } from "./Modes.jsx";

cytoscape.use(fcose);

// A pull request comment links here: ?repo=<name>&report=<id>
const LINKED = new URLSearchParams(window.location.search);
const LINKED_REPORT = /^[0-9a-f]{6,32}$/.test(LINKED.get("report") || "") ? LINKED.get("report") : null;

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
  const [mode, setMode] = useState(LINKED_REPORT ? "review" : "ask");
  const [opened, setOpened] = useState(null); // { report, markdown }
  const [reportError, setReportError] = useState("");
  const [cyVersion, setCyVersion] = useState(0);
  const [askMode, setAskModeState] = useState(() => {
    try {
      return modeById(localStorage.getItem("repodigger.mode") || "explore").id;
    } catch {
      return "explore";
    }
  });
  const [blastError, setBlastError] = useState("");
  const [graphVersion, setGraphVersion] = useState(0); // bumped when the open repo is re-indexed
  const indexedAt = useRef({});
  const endRef = useRef(null);
  const graphEl = useRef(null);

  useEffect(() => {
    applyTheme(theme);
    cyRef.current?.style(graphStyle());
  }, [theme]);

  function setAskMode(id) {
    setAskModeState(id);
    setBlastError("");
    try {
      localStorage.setItem("repodigger.mode", id);
    } catch {
      // a remembered mode is a convenience; the default works without it
    }
  }

  async function refreshAndAnalyse() {
    setBlastError("");
    try {
      await refreshRepo(repo, true);
      loadRepos();
    } catch (e) {
      setBlastError(e.message);
    }
  }

  // Repos that have a graph to show. One being indexed for the first time has none yet.
  const ready = repos.filter((r) => r.symbols > 0);
  const anyActive = repos.some((r) => r.status === "queued" || r.status === "running");

  function loadRepos(first = false) {
    return fetchRepos()
      .then((list) => {
        setRepos(list);
        const shown = list.filter((r) => r.symbols > 0);
        // a repo that finished re-indexing while open is redrawn
        list.forEach((r) => {
          const before = indexedAt.current[r.repo];
          if (before && r.indexed_at && r.indexed_at !== before && r.repo === repoRef.current) {
            setGraphVersion((v) => v + 1);
          }
          indexedAt.current[r.repo] = r.indexed_at;
        });
        setRepo((current) => {
          if (current && shown.some((r) => r.repo === current)) return current;
          const linked = shown.find((r) => r.repo === LINKED.get("repo"));
          return (linked || shown[0])?.repo || null;
        });
        if (shown.length === 0) {
          setStatus("No repository is indexed yet. Add one in the Repos tab.");
          if (first && !LINKED_REPORT) setMode("repos");
        }
      })
      .catch((e) => setStatus(`${e.message}. Check the API is running, then reload.`));
  }

  useEffect(() => {
    loadRepos(true);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // While anything is being indexed, follow it.
  useEffect(() => {
    if (!anyActive) return undefined;
    const timer = setInterval(() => loadRepos(), 3000);
    return () => clearInterval(timer);
  }, [anyActive]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (LINKED_REPORT) openReport(LINKED_REPORT);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const repoRef = useRef(repo);
  repoRef.current = repo;

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
        setCyVersion((v) => v + 1);
      })
      .catch((e) => setStatus(`${e.message}. Reload to try again.`));
    return () => {
      cancelled = true;
      cyRef.current?.destroy();
      cyRef.current = null;
    };
  }, [repo, graphVersion]);

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

  // The graph shows what was asked about, or the open report, whichever tab is up.
  useEffect(() => {
    if (!cyRef.current) return;
    clearPick();
    if (mode === "review" && opened?.report.repo === repo) {
      const { ids, path } = reportPath(opened.report);
      highlightPath(ids, path);
    } else if (mode === "ask" && active) {
      highlightPath(active.cited, active.path);
    } else {
      highlightPath([]);
    }
  }, [mode, opened, cyVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  async function openReport(id) {
    setReportError("");
    try {
      showReport(await fetchReport(id));
    } catch (e) {
      setReportError(e.message);
    }
  }

  function showReport(res) {
    if (repos.length > 0 && res.report.repo !== repo) setRepo(res.report.repo);
    setOpened(res);
  }

  function describe(id, known) {
    const n = cyRef.current?.getElementById(id);
    if (!n || n.empty()) return { name: id, kind: "file", summary: "", path: "", ...known };
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

  function pick(id, known) {
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
    setSelected({ id, ...describe(id, known), indexed: known?.indexed });
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
    setOpened(null);
    setSelected(null);
    setRepo(e.target.value);
  }

  async function onSubmit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy || !repo || !canAsk) return;
    const asked = askMode;
    setMessages((m) => [...m, { role: "user", text: q, mode: asked }]);
    setQuestion("");
    setBusy(true);
    setSelected(null);
    const started = Date.now();
    try {
      const res = await ask(q, repo, asked);
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

  const repoRow = repos.find((r) => r.repo === repo);
  const currentMode = modeById(askMode);
  // blast radius answers only from an analysed refresh; without one there is nothing to ask
  const canAsk = askMode !== "impact" || Boolean(repoRow?.blast);

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
        {mode === "review" && opened ? (
          <ImpactLog
            key={opened.report.id}
            report={opened.report}
            selectedId={selected?.id}
            onPick={pick}
            onClose={() => setOpened(null)}
          />
        ) : (
          <CoreLog
            core={mode === "ask" ? core : null}
            spend={active?.spend}
            busy={mode === "ask" && busy}
            elapsed={elapsed}
            counts={stats.counts}
            repo={repo}
            selectedId={selected?.id}
            onPick={pick}
            onWholeRepo={showWholeRepo}
            hint={mode === "review" ? "Open a report and its blast radius stacks here by hops from the change, with the same route lit in the graph." : ""}
          />
        )}
      </div>
      <section className="panel" aria-label="Questions">
        <header className="panel-head">
          <h1>RepoDigger</h1>
          <div className="panel-controls">
            {ready.length > 1 && (
              <select className="control" aria-label="Repository" value={repo || ""} onChange={onRepoChange} disabled={busy}>
                {ready.map((r) => (
                  <option key={r.repo} value={r.repo}>{r.repo}</option>
                ))}
              </select>
            )}
            {ready.length === 1 && <span className="repo-name">{ready[0].repo}</span>}
            <ThemeMenu theme={theme} onChange={setTheme} />
          </div>
        </header>
        <div className="tabs" role="tablist" aria-label="What to do">
          {[["ask", "Ask"], ["review", "Review"], ["repos", "Repos"]].map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`tab-${id}`}
              aria-selected={mode === id}
              aria-controls={`panel-${id}`}
              className={mode === id ? "on" : ""}
              onClick={() => setMode(id)}
            >
              {label}
            </button>
          ))}
        </div>
        {/* all stay mounted, so switching tabs never loses an answer */}
        <div className="tab-panel" id="panel-repos" role="tabpanel" aria-labelledby="tab-repos" hidden={mode !== "repos"}>
          <Repos
            repos={repos}
            current={repo}
            onOpen={(name) => {
              selectRepo(name);
              setMode("ask");
            }}
            onChanged={() => loadRepos()}
            analyze={askMode === "impact"}
          />
        </div>
        <div className="tab-panel" id="panel-review" role="tabpanel" aria-labelledby="tab-review" hidden={mode !== "review"}>
          <ReviewPanel
            repo={repo}
            opened={opened}
            error={reportError}
            onOpen={openReport}
            onShow={showReport}
            onBack={() => setOpened(null)}
            onPick={pick}
          />
        </div>
        <div className="tab-panel" id="panel-ask" role="tabpanel" aria-labelledby="tab-ask" hidden={mode !== "ask"}>
        <div className="mode-bar">
          <ModePicker mode={askMode} onChange={setAskMode} disabled={busy} />
          <small className="mode-hint">{currentMode.hint}</small>
        </div>
        <div className="messages">
          {askMode === "impact" && repo && (
            <BlastStatus
              row={repoRow}
              repo={repo}
              onRefresh={refreshAndAnalyse}
              refreshError={blastError}
              onOpenReport={(id) => {
                openReport(id);
                setMode("review");
              }}
            />
          )}
          {messages.length === 0 && (
            <p className="intro">{currentMode.intro(repo || "the codebase")}</p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.role === "assistant" ? (
                <>
                  <Answer text={m.text} />
                  {m !== active && m.cited.length > 0 && (
                    <button type="button" className="link" onClick={() => showAnswer(m)}>
                      Show this path
                    </button>
                  )}
                </>
              ) : m.role === "user" ? (
                <>
                  {m.mode && m.mode !== "explore" && <small className="msg-mode">{modeById(m.mode).label}</small>}
                  {m.text}
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
            placeholder={repo ? currentMode.placeholder(repo) : "Index a repository first"}
            aria-label={`Your question, ${currentMode.label} mode`}
            disabled={busy || !repo || !canAsk}
          />
          <button type="submit" className="ask" disabled={busy || !repo || !canAsk || !question.trim()}>
            {busy ? "Asking…" : "Ask"}
          </button>
        </form>
        </div>
      </section>
    </div>
  );
}
