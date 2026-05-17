import type {
  CatalogModel,
  FleetConfigOverride,
  FleetConfigResponse,
  MessagesPage,
  SessionRow,
} from "./types";

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
  createSession: (body: {
    provider: string;
    model: string;
    cwd?: string | null;
    additional_dirs?: string[] | null;
    title?: string;
    permission_mode?: string | null;
    fleet_config_override?: FleetConfigOverride | null;
  }) => json<SessionRow>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  fleetConfig: () => json<FleetConfigResponse>("/api/fleet/config"),
  systemCwd: () =>
    json<{
      cwd: string;
      home: string;
      allowed_roots: string[];
      permissive: boolean;
    }>("/api/system/cwd"),
  // Returns a page; for now ChatPane just unwraps `.messages` and ignores
  // pagination (the default page size of 50 covers a fresh chat). Older
  // history can be lazy-loaded via `before` later.
  getMessages: (id: string, opts?: { before?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (opts?.before) qs.set("before", opts.before);
    if (opts?.limit) qs.set("limit", String(opts.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return json<MessagesPage>(`/api/sessions/${id}/messages${suffix}`);
  },
  deleteSession: (id: string) =>
    fetch(`/api/sessions/${id}`, { method: "DELETE" }).then(() => undefined),
  deleteAllSessions: () =>
    fetch("/api/sessions", { method: "DELETE" }).then(() => undefined),
};

export function openSessionSocket(
  sessionId: string,
  sinceId?: number,
): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // `?since=<id>` lets the backend's session runner replay any events the
  // client missed during a brief disconnect. Omitted on first connect (the
  // client has nothing to resume from).
  const qs = typeof sinceId === "number" && sinceId > 0 ? `?since=${sinceId}` : "";
  return new WebSocket(
    `${proto}//${location.host}/api/sessions/${sessionId}/ws${qs}`,
  );
}
