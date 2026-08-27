import React, { useEffect, useRef, useState } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import { fetchGraph, ask } from "./api.js";

cytoscape.use(fcose);

// Color nodes by kind (file / class / function|method).
const KIND_COLOR = {
  file: "#8a5cf6",
  class: "#f59e0b",
  function: "#2f6df6",
  method: "#22a06b",
};

function buildElements(graph) {
  const ids = new Set(graph.nodes.map((n) => n.id));
  const nodes = graph.nodes.map((n) => ({
    data: {
      id: n.id,
      label: n.name || n.id,
      kind: n.kind,
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

export default function App() {
  const cyEl = useRef(null);
  const cyRef = useRef(null);
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchGraph()
      .then((graph) => {
        if (cancelled || !cyEl.current) return;
        const cy = cytoscape({
          container: cyEl.current,
          elements: buildElements(graph),
          style: [
            {
              selector: "node",
              style: {
                "background-color": (n) => KIND_COLOR[n.data("kind")] || "#94a3b8",
                width: (n) => 12 + Math.min(28, n.data("degree") * 3),
                height: (n) => 12 + Math.min(28, n.data("degree") * 3),
                label: "data(label)",
                "font-size": 7,
                color: "#384152",
                "text-valign": "bottom",
                "min-zoomed-font-size": 7,
              },
            },
            {
              selector: "edge",
              style: {
                width: 1,
                "line-color": "#cbd2dc",
                "target-arrow-color": "#cbd2dc",
                "target-arrow-shape": "triangle",
                "curve-style": "bezier",
                "arrow-scale": 0.6,
              },
            },
            { selector: ".dim", style: { opacity: 0.12 } },
            {
              selector: "node.lit",
              style: { "border-width": 3, "border-color": "#ef4444", opacity: 1 },
            },
            {
              selector: "edge.lit",
              style: { "line-color": "#ef4444", "target-arrow-color": "#ef4444", width: 2.5, opacity: 1 },
            },
          ],
          layout: { name: "fcose", animate: false, quality: "default", nodeSeparation: 80 },
        });
        cy.on("tap", "node", (evt) => {
          const d = evt.target.data();
          window.alert(`${d.id}\n\n${d.summary}`);
        });
        cyRef.current = cy;
      })
      .catch((e) => console.error(e));
    return () => {
      cancelled = true;
      cyRef.current?.destroy();
    };
  }, []);

  function highlightPath(ids) {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("lit").addClass("dim");
    if (!ids || ids.length === 0) {
      cy.elements().removeClass("dim");
      return;
    }
    const lit = cy.collection();
    ids.forEach((id) => {
      const n = cy.getElementById(id);
      if (n.nonempty()) lit.merge(n);
    });
    // light up edges between cited nodes
    lit.forEach((n) => {
      n.connectedEdges().forEach((e) => {
        if (lit.contains(e.source()) && lit.contains(e.target())) lit.merge(e);
      });
    });
    lit.removeClass("dim").addClass("lit");
    if (lit.nonempty()) cy.animate({ fit: { eles: lit, padding: 80 }, duration: 350 });
  }

  async function onSubmit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setMessages((m) => [...m, { role: "user", text: q }]);
    setQuestion("");
    setBusy(true);
    try {
      const res = await ask(q);
      setMessages((m) => [
        ...m,
        { role: "assistant", text: res.answer, cited: res.cited_ids || [] },
      ]);
      highlightPath(res.cited_ids);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", text: `Error: ${err.message}` }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <div className="graph">
        <div id="cy" ref={cyEl} />
        <div className="legend">
          <span><i style={{ background: KIND_COLOR.file }} />file</span>
          <span><i style={{ background: KIND_COLOR.class }} />class</span>
          <span><i style={{ background: KIND_COLOR.function }} />function</span>
          <span><i style={{ background: KIND_COLOR.method }} />method</span>
        </div>
      </div>
      <div className="panel">
        <h1>Codebase Explorer</h1>
        <div className="messages">
          {messages.length === 0 && (
            <div className="msg assistant">
              Ask a question about the indexed codebase, e.g. “how does authentication work?”.
              The traced path lights up in the graph.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.text}
              {m.cited && m.cited.length > 0 && (
                <div className="cited">
                  path:
                  <br />
                  {m.cited.map((id) => (
                    <code key={id} onClick={() => highlightPath([id])}>
                      {id}
                    </code>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
        <form onSubmit={onSubmit}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask about the codebase…"
            disabled={busy}
          />
          <button type="submit" disabled={busy}>
            {busy ? "…" : "Ask"}
          </button>
        </form>
      </div>
    </div>
  );
}
