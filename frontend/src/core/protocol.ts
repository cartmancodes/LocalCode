/**
 * The core RPC protocol — the same vocabulary `localcode --mode rpc` speaks on
 * stdio, carried over a WebSocket at /api/core/rpc.
 *
 * Commands go up, each answered by exactly one `response` echoing its `id`.
 * Events stream down as they happen. Extensions reach the user through
 * `extension_ui_request` / `extension_ui_response`, which is how an approval
 * prompt arrives.
 */

export type EngineName = "claude" | "codex" | "fake" | string;

export interface ModelRef {
  provider: string;
  modelId: string;
}

export interface SessionState {
  model: ModelRef | null;
  thinkingLevel: string;
  isStreaming: boolean;
  isCompacting: boolean;
  steeringMode: "all" | "one-at-a-time";
  followUpMode: "all" | "one-at-a-time";
  sessionFile: string | null;
  sessionId: string;
  sessionName?: string | null;
  autoCompactionEnabled: boolean;
  messageCount: number;
  pendingMessageCount: number;
  engine: EngineName;
  engineSessionId: string | null;
  quotaStatus: QuotaStatus;
  quotaHeadroom: number;
}

export type QuotaStatus = "ok" | "warning" | "exhausted" | "unknown";

export interface QuotaWindow {
  label: string;
  usedFraction: number;
  remainingFraction: number;
  durationMinutes: number | null;
  resetsAt: number | null;
  resetsInSeconds: number | null;
}

export interface QuotaReport {
  engine: EngineName;
  status: QuotaStatus;
  headroom: number;
  summary: string;
  engines: Record<string, { status: QuotaStatus; headroom: number; plan: string | null }>;
  current: {
    engine: string;
    plan: string | null;
    status: QuotaStatus;
    headroom: number;
    resetsInSeconds: number | null;
    windows: QuotaWindow[];
  } | null;
}

export interface SessionStats {
  sessionId: string;
  userMessages: number;
  assistantMessages: number;
  toolCalls: number;
  toolResults: number;
  totalMessages: number;
  tokens: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number };
  cost: number;
  quota: QuotaReport;
}

/** Commands the client can send. `id` correlates the response. */
export type Command =
  | { id?: string; type: "prompt"; message: string; streamingBehavior?: "steer" | "followUp" }
  | { id?: string; type: "steer"; message: string }
  | { id?: string; type: "follow_up"; message: string }
  | { id?: string; type: "abort" }
  | { id?: string; type: "clear_queue" }
  | { id?: string; type: "new_session" }
  | { id?: string; type: "get_state" }
  | { id?: string; type: "set_model"; provider: string; modelId: string }
  | { id?: string; type: "get_available_models" }
  | { id?: string; type: "set_thinking_level"; level: string }
  | { id?: string; type: "get_available_thinking_levels" }
  | { id?: string; type: "compact"; customInstructions?: string }
  | { id?: string; type: "get_session_stats" }
  | { id?: string; type: "get_quota" }
  | { id?: string; type: "get_packages" }
  | { id?: string; type: "get_messages" }
  | { id?: string; type: "get_entries"; since?: string }
  | { id?: string; type: "get_tree" }
  | { id?: string; type: "get_fork_messages" }
  | { id?: string; type: "fork"; entryId: string }
  | { id?: string; type: "get_commands" }
  | { id?: string; type: "set_session_name"; name: string }
  | { id?: string; type: "bash"; command: string };

export interface Response {
  type: "response";
  id?: string;
  command: string;
  success: boolean;
  data?: unknown;
  error?: string;
}

/** One delta inside a `message_update`. Mirrors pi's AssistantMessageEvent. */
export type AssistantMessageEvent =
  | { type: "text_start" | "thinking_start"; contentIndex: number }
  | { type: "text_delta" | "thinking_delta"; contentIndex: number; delta: string }
  | { type: "text_end" | "thinking_end"; contentIndex: number; content: string }
  | { type: "toolcall_start"; contentIndex: number; id: string; toolName: string }
  | { type: "toolcall_end"; contentIndex: number; toolCall: { id: string; name: string; arguments: unknown } }
  | { type: "done"; reason: string; message: unknown }
  | { type: "error"; reason: string; error: unknown };

export interface Usage {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  totalTokens: number;
  cost: { total: number };
}

export type ServerEvent =
  | { type: "ready"; state: SessionState }
  | { type: "agent_start" }
  | { type: "turn_start" }
  | { type: "message_start"; message: unknown }
  | { type: "message_update"; usage: Usage | null; assistantMessageEvent: AssistantMessageEvent }
  | { type: "message_end"; message: unknown }
  | { type: "tool_execution_start"; toolCallId: string; toolName: string; args: unknown }
  | { type: "tool_execution_update"; toolCallId: string; toolName: string; partialResult: { content?: Array<{ type: string; text?: string }> } }
  | { type: "tool_execution_end"; toolCallId: string; toolName: string; result: { content?: Array<{ type: string; text?: string }> }; isError: boolean }
  | { type: "turn_end"; message: unknown; toolResults: unknown[] }
  | { type: "agent_end"; messages: unknown[]; willRetry: boolean }
  | { type: "agent_settled" }
  | { type: "entry_appended"; entry: { id: string; type: string; [k: string]: unknown } }
  | { type: "queue_update"; steering: string[]; followUp: string[] }
  | { type: "compaction_start"; reason: string }
  | { type: "compaction_end"; reason: string; result: unknown; aborted: boolean }
  | { type: "rate_limit"; info: Record<string, unknown> }
  | { type: "model_changed"; model: ModelRef | null }
  | { type: "thinking_level_changed"; level: string }
  | { type: "session_info_changed"; name: string | null }
  | { type: "bash_execution_update"; id?: string; delta: string }
  | { type: "error"; message: string; willRetry?: boolean };

/** A dialog an extension is asking the user to answer. */
export type UIRequest =
  | { type: "extension_ui_request"; id: string; method: "select"; title: string; options: string[]; timeout?: number }
  | { type: "extension_ui_request"; id: string; method: "confirm"; title: string; message: string; timeout?: number }
  | { type: "extension_ui_request"; id: string; method: "input"; title: string; placeholder?: string; timeout?: number }
  | { type: "extension_ui_request"; id: string; method: "editor"; title: string; prefill?: string }
  | { type: "extension_ui_request"; id: string; method: "notify"; message: string; notifyType?: "info" | "warning" | "error" }
  | { type: "extension_ui_request"; id: string; method: "setStatus"; statusKey: string; statusText: string | null }
  | { type: "extension_ui_request"; id: string; method: "setWidget"; widgetKey: string; widgetLines: string[] | null }
  | { type: "extension_ui_request"; id: string; method: "setTitle"; title: string }
  | { type: "extension_ui_request"; id: string; method: "set_editor_text"; text: string };

export type UIResponse =
  | { type: "extension_ui_response"; id: string; value: string }
  | { type: "extension_ui_response"; id: string; confirmed: boolean }
  | { type: "extension_ui_response"; id: string; cancelled: true };

export type Inbound = ServerEvent | Response | UIRequest;

/** Dialog methods block the extension until answered; the rest are one-way. */
export const BLOCKING_UI_METHODS = ["select", "confirm", "input", "editor"] as const;

export function isDialog(request: UIRequest): boolean {
  return (BLOCKING_UI_METHODS as readonly string[]).includes(request.method);
}

export interface ConnectOptions {
  engine: EngineName;
  model?: string;
  cwd?: string;
  session?: "new" | "continue" | string;
  trustProject?: boolean;
  thinking?: string;
  permissionMode?: string;
  /** How a tool call the engine wants to run is resolved: ask the user, or
   *  decide without one. `ask` is the default and needs a UI attached. */
  permission?: "ask" | "allow" | "deny";
  models?: string[];
  inMemory?: boolean;
}

export function connectionUrl(options: ConnectOptions): string {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ engine: options.engine });
  if (options.model) query.set("model", options.model);
  if (options.cwd) query.set("cwd", options.cwd);
  if (options.session) query.set("session", options.session);
  if (options.trustProject) query.set("trust", "1");
  if (options.inMemory) query.set("memory", "1");
  if (options.thinking) query.set("thinking", options.thinking);
  if (options.permissionMode) query.set("permission_mode", options.permissionMode);
  if (options.permission) query.set("permission", options.permission);
  if (options.models?.length) query.set("models", options.models.join(","));
  return `${protocol}//${location.host}/api/core/rpc?${query.toString()}`;
}
