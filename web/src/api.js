// Single place that talks to the FastAPI backend. In dev, Vite proxies /api →
// localhost:8000; in the container, nginx proxies /api → the api service.
const BASE = "/api";

// Surface the API's own explanation ("daily token quota exhausted…") rather than a bare status.
async function unwrap(r, what) {
  if (r.ok) return r.json();
  let detail = "";
  try {
    const body = await r.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // not JSON: the status below is all there is
  }
  throw new Error(detail || `${what} failed with status ${r.status}`);
}

export async function fetchRepos() {
  return unwrap(await fetch(`${BASE}/repos`), "Loading repositories");
}

export async function fetchGraph(repo) {
  const query = repo ? `?repo=${encodeURIComponent(repo)}` : "";
  return unwrap(await fetch(`${BASE}/graph${query}`), "Loading the graph");
}

export async function fetchSource(id, repo) {
  const query = new URLSearchParams({ id, repo: repo || "" });
  return unwrap(await fetch(`${BASE}/symbol/source?${query}`), "Loading the code");
}

export async function ask(question, repo, mode = "explore") {
  const r = await fetch(`${BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, repo: repo || "", mode }),
  });
  return unwrap(r, "Asking");
}

export async function fetchImpactRange(repo) {
  const query = new URLSearchParams({ repo });
  return unwrap(await fetch(`${BASE}/impact/range?${query}`), "Loading commits");
}

export async function fetchReports(repo) {
  const query = new URLSearchParams({ repo });
  return unwrap(await fetch(`${BASE}/impact?${query}`), "Loading saved reports");
}

export async function fetchReport(id) {
  return unwrap(await fetch(`${BASE}/impact/${encodeURIComponent(id)}`), "Loading the report");
}

export async function runImpact({ repo, base, head }) {
  const r = await fetch(`${BASE}/impact`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo, base, head }),
  });
  return unwrap(r, "Analyzing");
}

export async function askReport(id, question) {
  const r = await fetch(`${BASE}/impact/${encodeURIComponent(id)}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  return unwrap(r, "Asking about the report");
}

export async function addRepo(url) {
  const r = await fetch(`${BASE}/repos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return unwrap(r, "Adding the repository");
}

export async function refreshRepo(name, analyze = false) {
  const r = await fetch(`${BASE}/repos/${encodeURIComponent(name)}/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ analyze }),
  });
  return unwrap(r, "Refreshing");
}
