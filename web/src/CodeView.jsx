import React, { useEffect, useRef } from "react";

// Shows one class or function exactly as stored in the graph. Repository code is
// untrusted, so it is only ever rendered as React text, never as HTML.
export default function CodeView({ code, onClose }) {
  const closeButton = useRef(null);

  useEffect(() => {
    closeButton.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const lines = code.source ? code.source.split("\n") : [];
  const span = code.start_line
    ? `${code.path}, lines ${code.start_line}–${code.end_line}`
    : code.path;

  return (
    <section className="code-view" aria-label="Source code">
      <header className="code-head">
        <div className="code-title">
          <code>{code.id}</code>
          {code.path && <small>{code.kind}, {span}</small>}
        </div>
        <button type="button" className="control" ref={closeButton} onClick={onClose}>
          Close code
        </button>
      </header>
      {code.loading && <p className="code-status" role="status">Loading the code…</p>}
      {code.error && <p className="code-status" role="alert">{code.error}</p>}
      {code.truncated && (
        <p className="code-note">This symbol is very long. Showing the first 200,000 characters.</p>
      )}
      {!code.loading && !code.error && lines.length === 0 && (
        <p className="code-status">No source is stored for this symbol.</p>
      )}
      {lines.length > 0 && (
        <pre className="code-body" tabIndex={0} style={{ "--first-line": (code.start_line || 1) - 1 }}>
          <code>
            {lines.map((line, i) => (
              <span className="code-line" key={i}>{line || " "}{"\n"}</span>
            ))}
          </code>
        </pre>
      )}
    </section>
  );
}
