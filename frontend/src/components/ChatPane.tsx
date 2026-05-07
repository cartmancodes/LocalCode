import { useEffect, useMemo, useRef, useState } from "react";
import { api, openSessionSocket } from "../api";
import type { ChatBlock, ChatTurn, SessionRow, StreamEvent } from "../types";
import Composer from "./Composer";
import MessageBubble from "./MessageBubble";

interface Props {
  session: SessionRow | null;
}

export default function ChatPane({ session }: Props) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const turnsRef = useRef<ChatTurn[]>([]);
  turnsRef.current = turns;
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

  // Open a fresh WS per active session.
  useEffect(() => {
    wsRef.current?.close();
    if (!session) return;
    const ws = openSessionSocket(session.id);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      let parsed: StreamEvent;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return;
      }
      handleEvent(parsed);
    };
    ws.onclose = () => setStreaming(false);
    return () => ws.close();
  }, [session?.id]);

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
    if (!session || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    setTurns((prev) => [
      ...prev,
      { role: "user", blocks: [{ kind: "text", text: prompt }] },
    ]);
    setStreaming(true);
    wsRef.current.send(JSON.stringify({ prompt }));
  };

  const headerLabel = useMemo(() => {
    if (!session) return "—";
    return `${session.provider} · ${session.model}`;
  }, [session]);

  return (
    <main className="chat-pane">
      <header className="chat-header">
        <div className="chat-title">{session?.title ?? "Pick a chat"}</div>
        <div className="chat-sub muted">{headerLabel}</div>
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
