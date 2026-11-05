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

export async function ask(question, repo) {
  const r = await fetch(`${BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, repo: repo || "" }),
  });
  return unwrap(r, "Asking");
}
