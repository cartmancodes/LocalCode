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
import CostMeter from "./CostMeter";
import CrewBar from "./CrewBar";
import { IconChevD, IconChevR, IconCheck, IconCopy } from "./icons";

interface Props {
  session: SessionRow | null;
  onConfigureFleet?: () => void;
}

type WsState = "connecting" | "open" | "closed";

const ROLE_COLORS: Record<FleetRole | "user" | "assistant", { fg: string; bg: string; bd: string }> = {
  planner:   { fg: "var(--ag-violet-fg)",  bg: "var(--ag-violet-bg)",  bd: "var(--ag-violet-bd)" },
  developer: { fg: "var(--ag-blue-fg)",    bg: "var(--ag-blue-bg)",    bd: "var(--ag-blue-bd)" },
  coder:     { fg: "var(--ag-emerald-fg)", bg: "var(--ag-emerald-bg)", bd: "var(--ag-emerald-bd)" },
  reviewer:  { fg: "var(--ag-amber-fg)",   bg: "var(--ag-amber-bg)",   bd: "var(--ag-amber-bd)" },
  user:      { fg: "var(--accent)",        bg: "var(--accent-bg)",     bd: "var(--accent-bd)" },
  assistant: { fg: "var(--ag-clay-fg)",    bg: "var(--ag-clay-bg)",    bd: "var(--ag-clay-bd)" },
};

/** Detect a fleet-style tool_use card label like "planner [claude:claude-sonnet-4-6]". */
function detectFleetRole(name?: string): FleetRole | null {
  if (!name) return null;
  const lower = name.toLowerCase();
  for (const r of ["planner", "developer", "coder", "reviewer"] as FleetRole[]) {
    if (lower.startsWith(r)) return r;
  }
  return null;
}

export default function ChatPane({ session, onConfigureFleet }: Props) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [wsState, setWsState] = useState<WsState>("closed");
  const [lastTurnCost, setLastTurnCost] = useState<number>(0);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const reconnectAttempt = useRef(0);
  const pendingSend = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Hydrate persisted messages whenever the active session changes.
  useEffect(() => {
    if (!session) {
      setTurns([]);
      setLastTurnCost(0);
      return;
    }
    let cancelled = false;
    (async () => {
      const page = await api.getMessages(session.id);
      if (cancelled) return;
      const hydrated: ChatTurn[] = page.messages.map((r: any) => ({
        role: r.role,
        blocks: (r.content ?? []).map((c: any) => blockFromPersisted(c)),
        costUsd: r.cost_usd ?? undefined,
        durationMs: r.duration_ms ?? undefined,
      }));
      setTurns(hydrated);
      // Best-effort session-cost initialisation: last assistant turn's cost.
      const lastAssistant = [...hydrated].reverse().find((t) => t.role === "assistant");
      setLastTurnCost(lastAssistant?.costUsd ?? 0);
    })();
    return () => {
      cancelled = true;
    };
  }, [session?.id]);

  // Auto-scroll on new content.
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns]);

  // Open / reconnect WS per active session.
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
      if (pendingSend.current != null) {
        const p = pendingSend.current;
        pendingSend.current = null;
        ws.send(JSON.stringify({ prompt: p }));
      }
    };

    ws.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as StreamEvent | { type: "ping"; data: any };
        if (parsed.type === "ping") return;
        handleEvent(parsed as StreamEvent);
      } catch {
        /* ignore malformed frame */
      }
    };

    const scheduleReconnect = () => {
      if (!session || wsRef.current !== ws) return;
      setStreaming(false);
      setWsState("closed");
      const delay = Math.min(1000 * 2 ** reconnectAttempt.current, 8000);
      reconnectAttempt.current += 1;
      reconnectTimer.current = window.setTimeout(() => connect(sessionId), delay);
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = () => undefined;
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
          if (typeof ev.data.cost_usd === "number") setLastTurnCost(ev.data.cost_usd);
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
    pendingSend.current = prompt;
    if (wsRef.current?.readyState !== WebSocket.CONNECTING) {
      teardown();
      connect(session.id);
    }
  };

  // For fleet sessions, pull the live config (incl. UI override merge) so the
  // crew bar reflects the actual roles in play.
  const [fleetBase, setFleetBase] = useState<FleetConfig | null>(null);
  useEffect(() => {
    if (session?.provider !== "fleet") {
      setFleetBase(null);
      return;
    }
    let cancelled = false;
    api
      .fleetConfig()
      .then((r) => {
        if (!cancelled) setFleetBase(r.config);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [session?.id, session?.provider]);

  const effectiveFleet = useMemo<FleetConfig | null>(() => {
    if (!session || session.provider !== "fleet" || !fleetBase) return null;
    return mergeFleetOverride(fleetBase, session.fleet_config_override);
  }, [session, fleetBase]);

  const [activeRole, setActiveRole] = useState<FleetRole | null>(null);
  // Reset filter when session changes.
  useEffect(() => setActiveRole(null), [session?.id]);

  return (
    <main className="lc-main">
      {effectiveFleet && session && (
        <CrewBar
          fleet={effectiveFleet}
          session={session}
          activeRole={activeRole}
          onPickRole={setActiveRole}
          onConfigure={onConfigureFleet}
        />
      )}
      {!effectiveFleet && session && (
        <div className="lc-chathead">
          <div className="lc-chathead__row">
            <span className="lc-chathead__eyebrow">{session.provider}</span>
            <h1 className="lc-chathead__title">{session.title}</h1>
            <span className="lc-chathead__sub">{session.model}</span>
            {session.cwd && (
              <span className="lc-chathead__cwd" title={session.cwd}>
                cwd: {session.cwd.replace(/^\/Users\/[^/]+/, "~")}
              </span>
            )}
            {session.additional_dirs && session.additional_dirs.length > 0 && (
              <span
                className="lc-chathead__cwd"
                title={`Additional dirs:\n${session.additional_dirs.join("\n")}`}
              >
                +{session.additional_dirs.length} dir
                {session.additional_dirs.length === 1 ? "" : "s"}
              </span>
            )}
          </div>
        </div>
      )}

      <div ref={scrollRef} className="lc-stream">
        {!session && (
          <div className="lc-stream__empty">
            <p>Pick a model in the sidebar and click <strong>+ New chat</strong> to start.</p>
          </div>
        )}
        {session && turns.length === 0 && (
          <div className="lc-stream__empty">
            <p>Send your first message below.</p>
          </div>
        )}
        {turns.map((t, i) => (
          <TurnView key={i} turn={t} activeRole={activeRole} />
        ))}
        {turns.length > 0 && !streaming && (
          <div className="lc-streamend">
            <span className="lc-streamend__line" />
            <span className="lc-streamend__lbl">end of turn</span>
            <span className="lc-streamend__line" />
          </div>
        )}
      </div>

      <div className="lc-bottom">
        <CostMeter sessionCostUsd={lastTurnCost} />
        <Composer
          session={session}
          disabled={!session || streaming || wsState !== "open"}
          onSend={send}
        />
      </div>
    </main>
  );
}

/* ─────────────────────────────────────────────────────────────────────── */
/*  Render a single turn (user prompt or assistant blocks).               */
/* ─────────────────────────────────────────────────────────────────────── */

function TurnView({ turn, activeRole }: { turn: ChatTurn; activeRole: FleetRole | null }) {
  if (turn.role === "user") {
    const text = turn.blocks
      .filter((b) => b.kind === "text")
      .map((b) => b.text ?? "")
      .join("");
    return (
      <div className="lc-msg lc-msg--user">
        <div className="lc-msg__rail">
          <div className="lc-msg__avatar lc-msg__avatar--user">SK</div>
        </div>
        <div className="lc-msg__body">
          <div className="lc-msg__head">
            <span className="lc-msg__name">You</span>
          </div>
          <div className="lc-bubble lc-bubble--user">{text}</div>
        </div>
      </div>
    );
  }

  // Assistant turn — group blocks into "rows" so a tool_use + its tool_result
  // render together with the same agent context. Filter by activeRole when set.
  type Row =
    | { kind: "text"; text: string }
    | { kind: "tool"; toolUse: ChatBlock; toolResult?: ChatBlock };
  const rows: Row[] = [];
  for (const b of turn.blocks) {
    if (b.kind === "text") {
      rows.push({ kind: "text", text: b.text ?? "" });
    } else if (b.kind === "tool_use") {
      rows.push({ kind: "tool", toolUse: b });
    } else if (b.kind === "tool_result") {
      const last = rows[rows.length - 1];
      if (
        last &&
        last.kind === "tool" &&
        !last.toolResult &&
        last.toolUse.toolUseId === b.toolUseId
      ) {
        last.toolResult = b;
      } else {
        // Orphan tool_result — render standalone.
        rows.push({
          kind: "tool",
          toolUse: { kind: "tool_use", toolName: "result", toolUseId: b.toolUseId },
          toolResult: b,
        });
      }
    }
  }

  // Filter by role if set.
  const filtered = activeRole
    ? rows.filter((r) => {
        if (r.kind !== "tool") return false; // hide free text when filtered to a role
        return detectFleetRole(r.toolUse.toolName) === activeRole;
      })
    : rows;

  return (
    <>
      {filtered.map((r, i) =>
        r.kind === "text" ? (
          r.text.trim() ? (
            <AgentTextRow key={i} text={r.text} inProgress={!!turn.inProgress && i === filtered.length - 1} />
          ) : null
        ) : (
          <AgentToolRow key={i} toolUse={r.toolUse} toolResult={r.toolResult} />
        )
      )}
      {turn.inProgress && (!filtered.length || filtered[filtered.length - 1]?.kind !== "text") && (
        <div className="lc-streamend">
          <span className="lc-streamend__line" />
          <span className="lc-streamend__lbl">streaming…</span>
          <span className="lc-streamend__line" />
        </div>
      )}
      {!turn.inProgress && (turn.costUsd != null || turn.durationMs != null) && (
        <div className="lc-msg__head" style={{ paddingLeft: 48, marginTop: -4 }}>
          {turn.durationMs != null && (
            <span className="lc-msg__dur">{(turn.durationMs / 1000).toFixed(2)}s</span>
          )}
          {turn.costUsd != null && (
            <span className="lc-msg__cost">${turn.costUsd.toFixed(4)}</span>
          )}
        </div>
      )}
    </>
  );
}

function AgentTextRow({ text, inProgress }: { text: string; inProgress: boolean }) {
  const colors = ROLE_COLORS.assistant;
  return (
    <div
      className="lc-msg lc-msg--agent"
      style={
        {
          "--agc-fg": colors.fg,
          "--agc-bg": colors.bg,
          "--agc-bd": colors.bd,
        } as React.CSSProperties
      }
    >
      <div className="lc-msg__rail">
        <div className="lc-msg__avatar">A</div>
      </div>
      <div className="lc-msg__body">
        <div className="lc-msg__head">
          <span className="lc-msg__name">Assistant</span>
          {inProgress && <span className="lc-spinner" />}
        </div>
        <div className="lc-text">{text}</div>
      </div>
    </div>
  );
}

function AgentToolRow({
  toolUse,
  toolResult,
}: {
  toolUse: ChatBlock;
  toolResult?: ChatBlock;
}) {
  const role = detectFleetRole(toolUse.toolName);
  const colors = role ? ROLE_COLORS[role] : ROLE_COLORS.assistant;
  const label = (toolUse.toolName ?? "tool").split(" ")[0]; // strip "[claude:...]" tail
  const detail = (toolUse.toolName ?? "").includes(" ")
    ? toolUse.toolName?.slice(label.length).trim()
    : "";

  return (
    <div
      className="lc-msg lc-msg--agent"
      style={
        {
          "--agc-fg": colors.fg,
          "--agc-bg": colors.bg,
          "--agc-bd": colors.bd,
        } as React.CSSProperties
      }
    >
      <div className="lc-msg__rail">
        <div className="lc-msg__avatar">{label.charAt(0).toUpperCase() || "T"}</div>
        <div className="lc-msg__line" />
      </div>
      <div className="lc-msg__body">
        <div className="lc-msg__head">
          <span className="lc-msg__name">{role ?? "tool"}</span>
          <span className="lc-msg__kind">{label}</span>
          {detail && (
            <>
              <span className="lc-msg__sep">·</span>
              <span className="lc-msg__title">{detail}</span>
            </>
          )}
        </div>

        {toolUse.toolInput != null && Object.keys(toolUse.toolInput || {}).length > 0 && (
          <CodeBlock
            data={toolUse.toolInput}
            lang="json"
            summary={`input: ${Object.keys(toolUse.toolInput).slice(0, 3).join(", ")}`}
            defaultOpen={false}
          />
        )}

        {toolResult && (
          <ToolResultView output={toolResult.toolOutput} isError={toolResult.isError} />
        )}
      </div>
    </div>
  );
}

function ToolResultView({ output, isError }: { output: any; isError?: boolean }) {
  if (output == null) return null;
  if (typeof output === "string") {
    if (output.length < 200 && !output.includes("\n")) {
      return (
        <div className={`lc-result ${isError ? "lc-result--error" : ""}`}>{output}</div>
      );
    }
    return (
      <CodeBlock
        data={output}
        lang="txt"
        defaultOpen={false}
        summary={isError ? "error" : `${output.split("\n").length} lines`}
      />
    );
  }
  return (
    <CodeBlock data={output} lang="json" defaultOpen={false} />
  );
}

function CodeBlock({
  data,
  lang = "json",
  defaultOpen = false,
  summary,
}: {
  data: any;
  lang?: string;
  defaultOpen?: boolean;
  summary?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [copied, setCopied] = useState(false);
  const text = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  const lines = text.split("\n").length;
  const sum =
    summary ??
    (lang === "json" && typeof data === "object" && data
      ? Object.keys(data).slice(0, 3).join(", ")
      : `${lines} lines`);

  const onCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="lc-code">
      <button className="lc-code__bar" onClick={() => setOpen((o) => !o)}>
        <span className="lc-code__chev">
          {open ? <IconChevD size={12} /> : <IconChevR size={12} />}
        </span>
        <span className="lc-code__lang">{lang}</span>
        <span className="lc-code__sum">{sum}</span>
        <span className="lc-code__lines">
          {lines} {lines === 1 ? "line" : "lines"}
        </span>
        <span className="lc-code__copy" onClick={onCopy} title="Copy">
          {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
        </span>
      </button>
      {open && (
        <pre className="lc-code__body">
          <code>{text}</code>
        </pre>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────── */
/*  Helpers                                                               */
/* ─────────────────────────────────────────────────────────────────────── */

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

function mergeFleetOverride(
  base: FleetConfig,
  override: SessionRow["fleet_config_override"]
): FleetConfig {
  if (!override) return base;

  // Mirror the backend's _merge_config: when `override.roles` is supplied
  // it REPLACES the workflow membership; otherwise the base's roles survive
  // and per-role fields merge on top.
  const merged: FleetConfig = {
    name: override.name ?? base.name,
    max_steps: override.max_steps ?? base.max_steps,
    entry_role: (override.entry_role ?? base.entry_role) as FleetRole,
    config_source: base.config_source,
    roles: {},
  };

  if (override.roles && Object.keys(override.roles).length > 0) {
    for (const role of Object.keys(override.roles) as FleetRole[]) {
      const o = override.roles[role]!;
      const baseRole = base.roles[role];
      // For added roles we don't have a base on the client; use whatever's
      // in the override and let the server's role library fill in any gaps.
      // For surviving roles, merge over the existing.
      if (baseRole) {
        merged.roles[role] = { ...baseRole, ...(o as Partial<FleetRoleConfig>) };
      } else {
        merged.roles[role] = {
          provider: (o.provider ?? "claude") as "claude" | "opencode",
          model: o.model ?? "",
          system_prompt: o.system_prompt ?? "",
        };
      }
    }
  } else {
    merged.roles = { ...base.roles };
  }

  // entry_role must be present in the workflow.
  const present = Object.keys(merged.roles) as FleetRole[];
  if (!present.includes(merged.entry_role)) {
    merged.entry_role = (present.find((r) => r !== "planner") ?? present[0] ?? "coder") as FleetRole;
  }

  return merged;
}
