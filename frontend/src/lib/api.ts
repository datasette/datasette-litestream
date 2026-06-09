import type { Status, ActionResult } from "./types";

// The admin page is served at "<base>/-/litestream". Derive the API base from
// the current path so the client works regardless of Datasette's base_url.
function adminBase(): string {
  return window.location.pathname.replace(/\/$/, "");
}

async function postJSON(path: string, body: unknown): Promise<ActionResult> {
  // Content-Type application/json makes Datasette's CSRF layer skip enforcement
  // for these same-origin fetches (asgi-csrf treats JSON posts as safe).
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let data: ActionResult;
  try {
    data = (await res.json()) as ActionResult;
  } catch {
    data = { ok: false, error: `HTTP ${res.status}` };
  }
  if (!res.ok && data.ok !== false) {
    data = { ok: false, error: data.error || `HTTP ${res.status}` };
  }
  return data;
}

export async function getStatus(): Promise<Status> {
  const res = await fetch(`${adminBase()}/api/status`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`status request failed: HTTP ${res.status}`);
  }
  return (await res.json()) as Status;
}

export function registerDatabase(database: string, replica?: string) {
  const body: Record<string, string> = { database };
  if (replica) body.replica = replica;
  return postJSON(`${adminBase()}/register`, body);
}

export function unregisterDatabase(database: string) {
  return postJSON(`${adminBase()}/unregister`, { database });
}

export function syncDatabase(database: string) {
  return postJSON(`${adminBase()}/api/sync`, { database });
}

export function startDatabase(database: string) {
  return postJSON(`${adminBase()}/api/start`, { database });
}

export function stopDatabase(database: string) {
  return postJSON(`${adminBase()}/api/stop`, { database });
}
