// Single place that talks to the FastAPI backend. In dev, Vite proxies /api →
// localhost:8000; in the container, nginx proxies /api → the api service.
const BASE = "/api";

export async function fetchGraph() {
  const r = await fetch(`${BASE}/graph`);
  if (!r.ok) throw new Error(`graph: ${r.status}`);
  return r.json();
}

export async function ask(question) {
  const r = await fetch(`${BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!r.ok) throw new Error(`ask: ${r.status}`);
  return r.json();
}
