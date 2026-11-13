import React from "react";
import { RiskLevel } from "./Impact.jsx";

// What kind of help a question asks for. Only the id goes to the server, which
// holds the system prompt for each.
export const ASK_MODES = [
  {
    id: "explore",
    label: "Explore",
    hint: "How the code works, traced through the graph.",
    placeholder: (repo) => `Ask how something in ${repo} works…`,
    intro: (repo) => `Ask how something in ${repo} works, for example “how does authentication work?”. The answer is traced through the graph, so you can check it.`,
  },
  {
    id: "refactor",
    label: "Refactor",
    hint: "Suggested changes, with what depends on them. You review and apply them; nothing is edited.",
    placeholder: () => "Which code needs refactoring, and how?",
    intro: (repo) => `Ask for refactoring suggestions in ${repo}, for example “simplify the order validation”. Each suggestion shows the code, what calls it, and what to test.`,
  },
  {
    id: "security",
    label: "Security",
    hint: "Follows untrusted input to risky operations. It assists a reviewer; it is not a scanner.",
    placeholder: () => "Check a flow for vulnerabilities…",
    intro: (repo) => `Ask for a security review in ${repo}, for example “can user input reach a SQL query unchecked?”. Findings cite the path through the code, and the answer says what it did not check.`,
  },
  {
    id: "impact",
    label: "Blast radius",
    hint: "What the latest analysed refresh changed, and everything it affects.",
    placeholder: () => "What could break from this change?",
    intro: () => "Ask about the change the latest refresh brought in: what it affects, what could break, what to test.",
  },
];

export const modeById = (id) => ASK_MODES.find((m) => m.id === id) || ASK_MODES[0];

export function ModePicker({ mode, onChange, disabled }) {
  return (
    <div className="mode-picker" role="radiogroup" aria-label="Mode">
      {ASK_MODES.map((m) => (
        <button
          key={m.id}
          type="button"
          role="radio"
          aria-checked={mode === m.id}
          className={mode === m.id ? "on" : ""}
          onClick={() => onChange(m.id)}
          disabled={disabled}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

const ACTIVE = ["queued", "running"];

// Blast radius needs a refresh that was analysed BEFORE the graph moved on. This
// says whether there is one for the open repo, and offers to run one.
export function BlastStatus({ row, repo, onRefresh, onOpenReport, refreshError }) {
  if (!row) return null;
  const running = ACTIVE.includes(row.status);
  const blast = row.blast;
  if (blast) {
    return (
      <div className="blast ready">
        <div className="blast-title">
          <span>Change ready to check</span>
          <RiskLevel level={blast.risk} />
        </div>
        <small>
          <code>{blast.base.slice(0, 7)}..{blast.head.slice(0, 7)}</code>: {blast.changed.toLocaleString()} changed,{" "}
          {blast.affected.toLocaleString()} affected
        </small>
        <button type="button" className="link" onClick={() => onOpenReport(blast.report_id)}>
          Open the full report
        </button>
      </div>
    );
  }
  return (
    <div className="blast">
      <p>No new symbols or code to check blast radius.</p>
      <small>
        Refresh {repo} in this mode after new commits arrive. The change is analysed before the graph is
        updated, because the old graph is the only record of what depended on the old code.
      </small>
      <button type="button" className="control" onClick={onRefresh} disabled={running}>
        {running ? "Refreshing…" : `Refresh ${repo} and analyse`}
      </button>
      {refreshError && <small className="repo-error">{refreshError}</small>}
    </div>
  );
}
