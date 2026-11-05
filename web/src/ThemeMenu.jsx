import React, { useEffect, useRef, useState } from "react";
import { THEMES } from "./themes.js";

function Swatches({ colors }) {
  return (
    <span className="swatches" aria-hidden="true">
      {colors.map((c) => (
        <i key={c} style={{ background: c }} />
      ))}
    </span>
  );
}

export default function ThemeMenu({ theme, onChange }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);
  const current = THEMES.find((t) => t.id === theme) || THEMES[0];

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onDown = (e) => {
      if (!root.current?.contains(e.target)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  return (
    <div className="theme-menu" ref={root}>
      <button
        type="button"
        className="control theme-button"
        aria-expanded={open}
        aria-label={`Theme: ${current.name}`}
        onClick={() => setOpen((v) => !v)}
      >
        <Swatches colors={current.swatches} />
        Theme
      </button>
      {open && (
        <fieldset className="theme-list">
          <legend>Theme</legend>
          {THEMES.map((t) => (
            <label key={t.id} className={t.id === theme ? "on" : ""}>
              <input
                type="radio"
                name="theme"
                value={t.id}
                checked={t.id === theme}
                onChange={() => onChange(t.id)}
              />
              <Swatches colors={t.swatches} />
              <span>
                <b>{t.name}</b>
                <small>{t.note}</small>
              </span>
            </label>
          ))}
        </fieldset>
      )}
    </div>
  );
}
