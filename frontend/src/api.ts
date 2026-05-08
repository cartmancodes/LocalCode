import type { Budget, CatalogModel, SessionRow } from "./types";

async function json<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const res = await fetch(input, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
  listModels: () => json<CatalogModel[]>("/api/models"),
  listSessions: () => json<SessionRow[]>("/api/sessions"),
  createSession: (body: { provider: string; model: string; cwd?: string; title?: string }) =>
    json<SessionRow>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  getMessages: (id: string) => json<any[]>(`/api/sessions/${id}/messages`),
  deleteSession: (id: string) =>
    fetch(`/api/sessions/${id}`, { method: "DELETE" }).then(() => undefined),
  deleteAllSessions: () =>
    fetch("/api/sessions", { method: "DELETE" }).then(() => undefined),
  budget: () => json<Budget>("/api/budget"),
};

export function openSessionSocket(sessionId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/api/sessions/${sessionId}/ws`);
}
