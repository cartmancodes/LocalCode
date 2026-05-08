import { useEffect, useMemo, useRef, useState } from "react";
import { api, openSessionSocket } from "../api";
import type {
  ChatBlock,
  ChatTurn,
  FleetConfig,
  FleetRole,
  FleetRoleConfig,
  SessionRow,
  StreamEvent,
} from "../types";
import Composer from "./Composer";
import MessageBubble from "./MessageBubble";

interface Props {
  session: SessionRow | null;
}

type WsState = "connecting" | "open" | "closed";

export default function ChatPane({ session }: Props) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [wsState, setWsState] = useState<WsState>("closed");
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const reconnectAttempt = useRef(0);
  const pendingSend = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Hydrate persisted messages whenever the active session changes.
  useEffect(() => {
    if (!session) {
      setTurns([]);
      return;
    }
    let cancelled = false;
    (async () => {
      const rows = await api.getMessages(session.id);
      if (cancelled) return;
      const hydrated: ChatTurn[] = rows.map((r) => ({
        role: r.role,
        blocks: (r.content ?? []).map((c: any) => blockFromPersisted(c)),
        costUsd: r.cost_usd ?? undefined,
        durationMs: r.duration_ms ?? undefined,
      }));
      setTurns(hydrated);
    })();
    return () => {
      cancelled = true;
    };
  }, [session?.id]);

  // Auto-scroll on new content.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  // Open a fresh WS per active session, with auto-reconnect on close.
  useEffect(() => {
    if (!session) {
      teardown();
      return;
    }
    reconnectAttempt.current = 0;
    connect(session.id);
    return () => {
      teardown();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  function teardown() {
    if (reconnectTimer.current != null) {
      window.clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    if (wsRef.current) {
      // Strip handlers so the close doesn't trigger a reconnect.
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.onmessage = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    setWsState("closed");
  }

  function connect(sessionId: string) {
    setWsState("connecting");
    const ws = openSessionSocket(sessionId);
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectAttempt.current = 0;
      setWsState("open");
      // If the user clicked Send while we were reconnecting, fire it now.
      if (pendingSend.current != null) {
        const p = pendingSend.current;
        pendingSend.current = null;
        ws.send(JSON.stringify({ prompt: p }));
      }
    };

    ws.onmessage = (ev) => {
      try {
        handleEvent(JSON.parse(ev.data) as StreamEvent);
      } catch {
        /* ignore malformed frame */
      }
    };

    const scheduleReconnect = () => {
      if (!session || wsRef.current !== ws) return; // session changed or torn down
      setStreaming(false);
      setWsState("closed");
      const delay = Math.min(1000 * 2 ** reconnectAttempt.current, 8000);
      reconnectAttempt.current += 1;
      reconnectTimer.current = window.setTimeout(() => connect(sessionId), delay);
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = () => {
      // onerror is followed by onclose, so we let scheduleReconnect handle it.
    };
  }

  const handleEvent = (ev: StreamEvent) => {
    setTurns((prev) => {
      const next = prev.slice();
      let cur = next[next.length - 1];
      const ensureAssistant = () => {
        if (!cur || cur.role !== "assistant" || !cur.inProgress) {
          cur = { role: "assistant", blocks: [], inProgress: true };
          next.push(cur);
        }
        return cur!;
      };

      switch (ev.type) {
        case "session.started":
          ensureAssistant();
          break;
        case "assistant.text": {
          const a = ensureAssistant();
          const last = a.blocks[a.blocks.length - 1];
          if (last && last.kind === "text") {
            last.text = (last.text ?? "") + ev.data.text;
          } else {
            a.blocks.push({ kind: "text", text: ev.data.text });
          }
          break;
        }
        case "assistant.tool_use": {
          const a = ensureAssistant();
          a.blocks.push({
            kind: "tool_use",
            toolUseId: ev.data.id,
            toolName: ev.data.name,
            toolInput: ev.data.input,
          });
          break;
        }
        case "tool.result": {
          const a = ensureAssistant();
          a.blocks.push({
            kind: "tool_result",
            toolUseId: ev.data.tool_use_id,
            toolOutput: ev.data.content,
            isError: ev.data.is_error,
          });
          break;
        }
        case "assistant.done": {
          const a = ensureAssistant();
          a.inProgress = false;
          a.costUsd = ev.data.cost_usd;
          a.durationMs = ev.data.duration_ms;
          setStreaming(false);
          break;
        }
        case "error": {
          const a = ensureAssistant();
          a.blocks.push({
            kind: "tool_result",
            toolOutput: ev.data.message,
            isError: true,
          });
          a.inProgress = false;
          setStreaming(false);
          break;
        }
      }
      return next;
    });
  };

  const send = (prompt: string) => {
    if (!session) return;
    setTurns((prev) => [
      ...prev,
      { role: "user", blocks: [{ kind: "text", text: prompt }] },
    ]);
    setStreaming(true);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ prompt }));
      return;
    }
    // Socket is connecting / closed: queue the send and force a reconnect now
    // rather than waiting on backoff.
    pendingSend.current = prompt;
    if (wsRef.current?.readyState !== WebSocket.CONNECTING) {
      teardown();
      connect(session.id);
    }
  };

  const headerLabel = useMemo(() => {
    if (!session) return "—";
    return `${session.provider} · ${session.model}`;
  }, [session]);

  // For fleet sessions, fetch the global config once and merge the per-session
  // override locally so the header can show what's actually running.
  const [fleetBase, setFleetBase] = useState<FleetConfig | null>(null);
  useEffect(() => {
    if (session?.provider !== "fleet") return;
    let cancelled = false;
    api.fleetConfig().then((r) => {
      if (!cancelled) setFleetBase(r.config);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [session?.id, session?.provider]);

  const effectiveFleet = useMemo<FleetConfig | null>(() => {
    if (!session || session.provider !== "fleet" || !fleetBase) return null;
    return mergeFleetOverride(fleetBase, session.fleet_config_override);
  }, [session, fleetBase]);

  return (
    <main className="chat-pane">
      <header className="chat-header">
        <div className="chat-title">{session?.title ?? "Pick a chat"}</div>
        <div className="chat-sub muted">
          {headerLabel}
          {session && wsState !== "open" && (
            <span className={`ws-pill ws-${wsState}`}>
              {wsState === "connecting" ? " · reconnecting…" : " · disconnected"}
            </span>
          )}
        </div>
        {effectiveFleet && (
          <div className="chat-fleet-roles">
            {(["planner", "developer", "coder", "reviewer"] as FleetRole[]).map((role) => {
              const r = effectiveFleet[role];
              const overridden = isRoleOverridden(role, session?.fleet_config_override);
              return (
                <span
                  key={role}
                  className={`fleet-role-chip ${overridden ? "is-overridden" : ""}`}
                  title={`${role}: ${r.provider}:${r.model}${overridden ? " (UI override)" : ""}`}
                >
                  <span className="fleet-role-chip-name">{role}</span>
                  <span className="fleet-role-chip-model">
                    {r.provider}:{r.model}
                  </span>
                </span>
              );
            })}
          </div>
        )}
      </header>

      <div ref={scrollRef} className="chat-scroll">
        {turns.map((t, i) => (
          <MessageBubble key={i} turn={t} />
        ))}
        {!session && <div className="empty muted">Create a new chat to get started.</div>}
      </div>

      <Composer disabled={!session || streaming} onSend={send} />
    </main>
  );
}

function mergeFleetOverride(
  base: FleetConfig,
  override: SessionRow["fleet_config_override"]
): FleetConfig {
  if (!override) return base;
  const merged: FleetConfig = JSON.parse(JSON.stringify(base));
  if (override.max_steps != null) merged.max_steps = override.max_steps;
  if (override.name) merged.name = override.name;
  for (const role of ["planner", "developer", "coder", "reviewer"] as FleetRole[]) {
    const o = override.roles?.[role];
    if (!o) continue;
    merged[role] = { ...merged[role], ...(o as Partial<FleetRoleConfig>) };
  }
  return merged;
}

function isRoleOverridden(
  role: FleetRole,
  override: SessionRow["fleet_config_override"]
): boolean {
  if (!override?.roles?.[role]) return false;
  const o = override.roles[role]!;
  return Boolean(o.provider || o.model || o.system_prompt);
}

function blockFromPersisted(b: any): ChatBlock {
  if (b?.type === "text") return { kind: "text", text: b.text };
  if (b?.type === "tool_use") {
    return { kind: "tool_use", toolUseId: b.id, toolName: b.name, toolInput: b.input };
  }
  if (b?.type === "tool_result") {
    return {
      kind: "tool_result",
      toolUseId: b.tool_use_id,
      toolOutput: b.content,
      isError: b.is_error,
    };
  }
  return { kind: "text", text: typeof b === "string" ? b : JSON.stringify(b) };
}
