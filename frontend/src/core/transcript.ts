/**
 * Turning the event stream into a transcript.
 *
 * The reducer is pure and lives apart from React so the mapping from protocol
 * events to rendered turns can be reasoned about (and tested) on its own.
 */

import type { QuotaStatus, ServerEvent, Usage } from "./protocol";

export interface TextBlock {
  kind: "text";
  text: string;
}

export interface ThinkingBlock {
  kind: "thinking";
  text: string;
}

export interface ToolBlock {
  kind: "tool";
  id: string;
  name: string;
  args: unknown;
  output: string;
  isError: boolean;
  running: boolean;
}

export type Block = TextBlock | ThinkingBlock | ToolBlock;

export interface Turn {
  id: string;
  role: "user" | "assistant";
  blocks: Block[];
  inProgress: boolean;
  usage?: Usage | null;
}

export interface TranscriptState {
  turns: Turn[];
  streaming: boolean;
  compacting: boolean;
  queued: { steering: string[]; followUp: string[] };
  quota: { status: QuotaStatus; headroom: number } | null;
  error: string | null;
}

export const emptyTranscript = (): TranscriptState => ({
  turns: [],
  streaming: false,
  compacting: false,
  queued: { steering: [], followUp: [] },
  quota: null,
  error: null,
});

let counter = 0;
const nextId = () => `t${++counter}`;

export function appendUserTurn(state: TranscriptState, text: string): TranscriptState {
  return {
    ...state,
    error: null,
    streaming: true,
    turns: [
      ...state.turns,
      { id: nextId(), role: "user", blocks: [{ kind: "text", text }], inProgress: false },
    ],
  };
}

/** Fold one protocol event into the transcript. */
export function reduce(state: TranscriptState, event: ServerEvent): TranscriptState {
  switch (event.type) {
    case "agent_start":
      return { ...state, streaming: true, error: null };

    case "message_start":
      return { ...state, turns: [...state.turns, newAssistantTurn()] };

    case "message_update":
      return updateLastAssistant(state, (turn) => applyDelta(turn, event));

    case "message_end":
      return updateLastAssistant(state, (turn) => ({ ...turn, inProgress: false }));

    case "tool_execution_start":
      return updateLastAssistant(state, (turn) => ({
        ...turn,
        blocks: upsertTool(turn.blocks, event.toolCallId, {
          kind: "tool",
          id: event.toolCallId,
          name: event.toolName,
          args: event.args,
          output: "",
          isError: false,
          running: true,
        }),
      }));

    case "tool_execution_update":
      return updateLastAssistant(state, (turn) => ({
        ...turn,
        blocks: turn.blocks.map((block) =>
          block.kind === "tool" && block.id === event.toolCallId
            ? { ...block, output: block.output + textOf(event.partialResult?.content) }
            : block
        ),
      }));

    case "tool_execution_end":
      return updateLastAssistant(state, (turn) => ({
        ...turn,
        blocks: turn.blocks.map((block) =>
          block.kind === "tool" && block.id === event.toolCallId
            ? {
                ...block,
                output: textOf(event.result?.content) || block.output,
                isError: event.isError,
                running: false,
              }
            : block
        ),
      }));

    case "agent_settled":
      return {
        ...state,
        streaming: false,
        turns: state.turns.map((turn) => (turn.inProgress ? { ...turn, inProgress: false } : turn)),
      };

    case "queue_update":
      return { ...state, queued: { steering: event.steering, followUp: event.followUp } };

    case "compaction_start":
      return { ...state, compacting: true };

    case "compaction_end":
      return { ...state, compacting: false };

    case "rate_limit":
      return state; // the authoritative figure comes from get_quota

    case "error":
      return {
        ...state,
        error: event.message,
        streaming: event.willRetry ? state.streaming : false,
      };

    default:
      return state;
  }
}

function newAssistantTurn(): Turn {
  return { id: nextId(), role: "assistant", blocks: [], inProgress: true };
}

function updateLastAssistant(
  state: TranscriptState,
  update: (turn: Turn) => Turn
): TranscriptState {
  const turns = state.turns.slice();
  let index = turns.length - 1;
  while (index >= 0 && turns[index].role !== "assistant") index -= 1;
  if (index < 0) {
    turns.push(update(newAssistantTurn()));
  } else {
    turns[index] = update(turns[index]);
  }
  return { ...state, turns };
}

function applyDelta(turn: Turn, event: Extract<ServerEvent, { type: "message_update" }>): Turn {
  const delta = event.assistantMessageEvent;
  const usage = event.usage ?? turn.usage;
  switch (delta.type) {
    case "text_delta":
      return { ...turn, usage, blocks: appendText(turn.blocks, "text", delta.delta) };
    case "thinking_delta":
      return { ...turn, usage, blocks: appendText(turn.blocks, "thinking", delta.delta) };
    case "toolcall_start":
      return {
        ...turn,
        usage,
        blocks: upsertTool(turn.blocks, delta.id, {
          kind: "tool",
          id: delta.id,
          name: delta.toolName,
          args: undefined,
          output: "",
          isError: false,
          running: true,
        }),
      };
    case "toolcall_end":
      return {
        ...turn,
        usage,
        blocks: turn.blocks.map((block) =>
          block.kind === "tool" && block.id === delta.toolCall.id
            ? { ...block, args: delta.toolCall.arguments }
            : block
        ),
      };
    default:
      return { ...turn, usage };
  }
}

/** Text and thinking accumulate into the trailing block of that kind. */
function appendText(blocks: Block[], kind: "text" | "thinking", delta: string): Block[] {
  const last = blocks[blocks.length - 1];
  if (last && last.kind === kind) {
    const updated = blocks.slice();
    updated[updated.length - 1] = { ...last, text: last.text + delta };
    return updated;
  }
  return [...blocks, kind === "text" ? { kind, text: delta } : { kind, text: delta }];
}

/** A tool block may be announced twice (stream delta, then execution start). */
function upsertTool(blocks: Block[], id: string, block: ToolBlock): Block[] {
  const index = blocks.findIndex((b) => b.kind === "tool" && b.id === id);
  if (index < 0) return [...blocks, block];
  const existing = blocks[index] as ToolBlock;
  const updated = blocks.slice();
  updated[index] = {
    ...existing,
    name: block.name || existing.name,
    args: block.args ?? existing.args,
    running: block.running,
  };
  return updated;
}

function textOf(content: Array<{ type: string; text?: string }> | undefined): string {
  if (!content) return "";
  return content
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("");
}
