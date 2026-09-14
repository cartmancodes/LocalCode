/**
 * The chat surface on the core RPC protocol.
 *
 * Differences from the legacy pane, all of them consequences of the protocol:
 * approvals arrive as `extension_ui_request` dialogs rather than a bespoke
 * pipeline event, quota headroom is a first-class readout, and thinking blocks
 * render because the engines actually stream them.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CoreClient, type ConnectionState } from "../core/client";
import type {
  ConnectOptions,
  QuotaReport,
  SessionState,
  UIRequest,
} from "../core/protocol";
import { isDialog } from "../core/protocol";
import {
  appendUserTurn,
  emptyTranscript,
  reduce,
  type Block,
  type TranscriptState,
} from "../core/transcript";
import Composer from "./Composer";
import { IconCheck, IconChevD, IconChevR, IconCopy } from "./icons";

interface Props {
  options: ConnectOptions | null;
  onSessionState?: (state: SessionState) => void;
}

interface Toast {
  id: string;
  message: string;
  kind: "info" | "warning" | "error";
}

const STICKY_BOTTOM_PX = 120;

export default function CorePane({ options, onSessionState }: Props) {
  const [connection, setConnection] = useState<ConnectionState>("closed");
  const [detail, setDetail] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptState>(emptyTranscript);
  const [state, setState] = useState<SessionState | null>(null);
  const [quota, setQuota] = useState<QuotaReport | null>(null);
  const [dialog, setDialog] = useState<UIRequest | null>(null);
  const [dialogInput, setDialogInput] = useState("");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [status, setStatus] = useState<Record<string, string>>({});

  const clientRef = useRef<CoreClient | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);

  const pushToast = useCallback((message: string, kind: Toast["kind"]) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((cur) => [...cur, { id, message, kind }]);
    window.setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== id)), 6000);
  }, []);

  const handleUIRequest = useCallback(
    (request: UIRequest) => {
      if (isDialog(request)) {
        setDialogInput(request.method === "editor" ? (request.prefill ?? "") : "");
        setDialog(request);
        return;
      }
      switch (request.method) {
        case "notify":
          pushToast(request.message, request.notifyType ?? "info");
          break;
        case "setStatus":
          setStatus((cur) => {
            const next = { ...cur };
            if (request.statusText) next[request.statusKey] = request.statusText;
            else delete next[request.statusKey];
            return next;
          });
          break;
        default:
          break; // setWidget / setTitle / set_editor_text have no home here yet
      }
    },
    [pushToast]
  );

  // One connection per set of options; a dropped socket ends the session
  // rather than silently reconnecting into a different one.
  useEffect(() => {
    if (!options) return;
    setTranscript(emptyTranscript());
    setState(null);
    setQuota(null);
    const client = new CoreClient({
      onEvent: (event) => {
        setTranscript((cur) => reduce(cur, event));
        if (event.type === "ready") {
          setState(event.state);
          onSessionState?.(event.state);
        }
        if (event.type === "agent_settled" || event.type === "rate_limit") {
          void refreshQuota(client);
        }
      },
      onUIRequest: handleUIRequest,
      onState: (next, why) => {
        setConnection(next);
        setDetail(why ?? null);
      },
    });
    clientRef.current = client;
    client.connect(options);
    return () => {
      client.disconnect();
      clientRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options, handleUIRequest]);

  const refreshQuota = async (client: CoreClient) => {
    try {
      setQuota(await client.send<QuotaReport>({ type: "get_quota" }));
    } catch {
      /* quota is advisory; a failed read should not interrupt the chat */
    }
  };

  // Follow the tail unless the user has scrolled up to read.
  useEffect(() => {
    const element = scrollRef.current;
    if (element && stick.current) element.scrollTop = element.scrollHeight;
  }, [transcript]);

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    stick.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < STICKY_BOTTOM_PX;
  };

  const send = async (text: string) => {
    const client = clientRef.current;
    if (!client?.connected) return;
    setTranscript((cur) => appendUserTurn(cur, text));
    try {
      await client.send({
        type: "prompt",
        message: text,
        ...(transcript.streaming ? { streamingBehavior: "followUp" as const } : {}),
      });
    } catch (error) {
      setTranscript((cur) => ({
        ...cur,
        streaming: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    }
  };

  const abort = async () => {
    try {
      await clientRef.current?.send({ type: "abort" });
    } catch {
      /* aborting an already-finished turn is not an error worth showing */
    }
  };

  const answerDialog = (answer: { value?: string; confirmed?: boolean; cancelled?: boolean }) => {
    const client = clientRef.current;
    if (!client || !dialog) return;
    if (answer.cancelled) client.respondToUI({ type: "extension_ui_response", id: dialog.id, cancelled: true });
    else if (answer.confirmed !== undefined)
      client.respondToUI({ type: "extension_ui_response", id: dialog.id, confirmed: answer.confirmed });
    else client.respondToUI({ type: "extension_ui_response", id: dialog.id, value: answer.value ?? "" });
    setDialog(null);
    setDialogInput("");
  };

  const statusLine = useMemo(() => Object.values(status).join(" · "), [status]);

  if (!options) {
    return (
      <main className="lc-chat">
        <div className="lc-empty">Pick an engine to start a session.</div>
      </main>
    );
  }

  return (
    <main className="lc-chat">
      <CoreHeader
        connection={connection}
        detail={detail}
        state={state}
        quota={quota}
        statusLine={statusLine}
        queued={transcript.queued}
        compacting={transcript.compacting}
      />

      <div className="lc-scroll" ref={scrollRef} onScroll={onScroll}>
        {transcript.turns.map((turn) => (
          <article key={turn.id} className={`lc-turn lc-turn--${turn.role}`}>
            {turn.blocks.map((block, index) => (
              <BlockView key={index} block={block} />
            ))}
            {turn.inProgress && <span className="lc-caret" aria-label="generating" />}
            {turn.usage && !turn.inProgress && (
              <footer className="lc-turn-meta">
                {turn.usage.totalTokens.toLocaleString()} tokens
                {turn.usage.cacheRead > 0 && ` · ${turn.usage.cacheRead.toLocaleString()} cached`}
              </footer>
            )}
          </article>
        ))}
        {transcript.error && <div className="lc-error">{transcript.error}</div>}
      </div>

      {dialog && (
        <DialogView
          request={dialog}
          value={dialogInput}
          onChange={setDialogInput}
          onAnswer={answerDialog}
        />
      )}

      <div className="lc-toasts">
        {toasts.map((toast) => (
          <div key={toast.id} className={`lc-toast lc-toast--${toast.kind}`}>
            {toast.message}
          </div>
        ))}
      </div>

      <Composer session={null} disabled={connection !== "open"} onSend={send} />
      {transcript.streaming && (
        <button className="lc-stop" onClick={abort}>
          Stop
        </button>
      )}
    </main>
  );
}

function CoreHeader({
  connection,
  detail,
  state,
  quota,
  statusLine,
  queued,
  compacting,
}: {
  connection: ConnectionState;
  detail: string | null;
  state: SessionState | null;
  quota: QuotaReport | null;
  statusLine: string;
  queued: { steering: string[]; followUp: string[] };
  compacting: boolean;
}) {
  const pending = queued.steering.length + queued.followUp.length;
  return (
    <header className="lc-core-head">
      <span className={`lc-dot lc-dot--${connection}`} title={detail ?? connection} />
      <span className="lc-core-engine">
        {state ? `${state.engine}${state.model ? ` · ${state.model.modelId}` : ""}` : connection}
      </span>
      {quota && quota.status !== "unknown" && <QuotaBadge quota={quota} />}
      {compacting && <span className="lc-chip">compacting…</span>}
      {pending > 0 && <span className="lc-chip">{pending} queued</span>}
      {statusLine && <span className="lc-core-status">{statusLine}</span>}
    </header>
  );
}

function QuotaBadge({ quota }: { quota: QuotaReport }) {
  const percent = Math.round(quota.headroom * 100);
  const resets = quota.current?.resetsInSeconds;
  const title = resets
    ? `${quota.summary} — resets in ${Math.ceil(resets / 60)} min`
    : quota.summary;
  return (
    <span className={`lc-core-quota lc-core-quota--${quota.status}`} title={title}>
      {percent}% left
    </span>
  );
}

function BlockView({ block }: { block: Block }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  if (block.kind === "text") {
    return (
      <div className="lc-block lc-block--text">
        {block.text}
        <button
          className="lc-copy"
          onClick={() => {
            void navigator.clipboard.writeText(block.text);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
          title="Copy"
        >
          {copied ? <IconCheck /> : <IconCopy />}
        </button>
      </div>
    );
  }

  if (block.kind === "thinking") {
    return (
      <details className="lc-block lc-block--thinking">
        <summary>thinking</summary>
        <pre>{block.text}</pre>
      </details>
    );
  }

  return (
    <div className={`lc-block lc-block--tool ${block.isError ? "is-error" : ""}`}>
      <button className="lc-tool-head" onClick={() => setOpen((o) => !o)}>
        {open ? <IconChevD /> : <IconChevR />}
        <span className="lc-tool-name">{block.name}</span>
        {block.running && <span className="lc-chip">running</span>}
        {block.isError && <span className="lc-chip lc-chip--error">error</span>}
      </button>
      {open && (
        <div className="lc-tool-body">
          {block.args !== undefined && <pre className="lc-tool-args">{JSON.stringify(block.args, null, 2)}</pre>}
          {block.output && <pre className="lc-tool-output">{block.output}</pre>}
        </div>
      )}
    </div>
  );
}

function DialogView({
  request,
  value,
  onChange,
  onAnswer,
}: {
  request: UIRequest;
  value: string;
  onChange: (value: string) => void;
  onAnswer: (answer: { value?: string; confirmed?: boolean; cancelled?: boolean }) => void;
}) {
  const title = "title" in request ? request.title : "";
  return (
    <div className="lc-dialog" role="dialog" aria-label={title}>
      <h3>{title}</h3>
      {request.method === "confirm" && <p>{request.message}</p>}
      {request.method === "select" && (
        <div className="lc-dialog-options">
          {request.options.map((option) => (
            <button key={option} onClick={() => onAnswer({ value: option })}>
              {option}
            </button>
          ))}
        </div>
      )}
      {(request.method === "input" || request.method === "editor") && (
        <textarea
          autoFocus
          rows={request.method === "editor" ? 8 : 2}
          value={value}
          placeholder={request.method === "input" ? request.placeholder : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      <div className="lc-dialog-actions">
        <button onClick={() => onAnswer({ cancelled: true })}>Cancel</button>
        {request.method === "confirm" ? (
          <>
            <button onClick={() => onAnswer({ confirmed: false })}>No</button>
            <button className="lc-primary" onClick={() => onAnswer({ confirmed: true })}>
              Yes
            </button>
          </>
        ) : request.method !== "select" ? (
          <button className="lc-primary" onClick={() => onAnswer({ value })}>
            Submit
          </button>
        ) : null}
      </div>
    </div>
  );
}
