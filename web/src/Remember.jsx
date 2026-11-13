import React from "react";

// One switch for every mode: whether earlier questions in this conversation are
// sent with each new one. Off, every question stands alone, as before.
export default function Remember({ on, onToggle, sessions, sessionId, onResume, onNew, onDelete, disabled }) {
  return (
    <div className="remember">
      <label className="switch">
        <input type="checkbox" role="switch" checked={on} onChange={(e) => onToggle(e.target.checked)} disabled={disabled} />
        <span className="track" aria-hidden="true"><span className="thumb" /></span>
        Remember conversation
      </label>
      {on && (
        <div className="remember-tools">
          {sessions.length > 0 && (
            <select
              className="control"
              aria-label="Resume a conversation"
              value={sessionId || ""}
              onChange={(e) => (e.target.value ? onResume(e.target.value) : onNew())}
              disabled={disabled}
            >
              <option value="">New conversation</option>
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title || "Untitled"} ({s.turns})
                </option>
              ))}
            </select>
          )}
          {sessionId && (
            <>
              <button type="button" className="link" onClick={onNew} disabled={disabled}>New</button>
              <button type="button" className="link" onClick={onDelete} disabled={disabled}>Delete</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
