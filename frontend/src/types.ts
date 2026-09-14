// "core" is not a backend provider row — those entries open a session on the
// core RPC protocol at /api/core/rpc, where the loop runs in the vendor binary.
export type Provider = "claude" | "codex" | "opencode" | "fleet" | "core";

// Permission/auto mode forwarded to the upstream agent (Claude:
// acceptEdits / default / plan / bypassPermissions). Chosen per-session.
export type PermissionMode =
  | "acceptEdits"
  | "default"
  | "plan"
  | "bypassPermissions";

export const PERMISSION_MODES: { id: PermissionMode; label: string; hint: string }[] = [
  { id: "acceptEdits", label: "Auto (accept edits)", hint: "Agent edits files without asking — default" },
  { id: "default", label: "Ask", hint: "Agent uses default permission prompts" },
  { id: "plan", label: "Plan only", hint: "Agent plans but does not modify files" },
  { id: "bypassPermissions", label: "Full auto (bypass)", hint: "Bypass all permission checks — use with care" },
];

export interface CatalogModel {
  id: string;
  provider: Provider;
  model: string;
}

export interface SessionRow {
  id: string;
  title: string;
  provider: Provider;
  model: string;
  cwd: string | null;
  /** Extra absolute paths the agent's tools may operate on beyond `cwd`. */
  additional_dirs: string[] | null;
  upstream_id: string | null;
  permission_mode: PermissionMode | null;
  fleet_config_override: FleetConfigOverride | null;
  created_at: string;
  updated_at: string;
}

export interface MessagesPage {
  messages: any[];
  next_before: string | null;
  has_more: boolean;
}

// Mirrors the backend's FleetConfig — what /api/fleet/config returns.
//
// Workflow membership is presence-based: only roles in `roles` are part of
// this workflow. Adding/removing an agent literally adds/removes a key. There
// is no "disabled" flag.
export type FleetRole = "planner" | "developer" | "coder" | "tester" | "reviewer";

export interface FleetRoleConfig {
  provider: "claude" | "codex" | "opencode";
  model: string;
  system_prompt: string;
}

export interface FleetConfig {
  name: string;
  roles: Partial<Record<FleetRole, FleetRoleConfig>>;
  entry_role: FleetRole;
  max_steps: number;
  /** Auto-retry budget when the reviewer NACKs. 0 disables retries. */
  max_review_retries: number;
  /** When true (and a planner is present), the workflow pauses after the
   *  planner emits its plan and waits for user approve/reject. */
  require_plan_approval: boolean;
  /** When true, every registered agent runs on every turn (the pre-routing
   *  behaviour). Off by default: trivia gets routed to one agent. */
  always_full_crew: boolean;
  config_source: string | null;
}

export interface WorkflowPreset {
  label: string;
  description: string;
  roles: FleetRole[];
  entry_role: FleetRole;
}

export interface FleetConfigResponse {
  config: FleetConfig;
  is_default: boolean;
  valid_providers: ("claude" | "codex" | "opencode")[];
  valid_roles: FleetRole[];
  role_library: Record<FleetRole, FleetRoleConfig>;
  presets: Record<string, WorkflowPreset>;
  defaults: FleetConfig;
}

// Partial override — when `roles` is supplied, it REPLACES the workflow
// membership. Per-role fields fall back to the role library so writing
// `coder: { model: "..." }` doesn't require re-specifying everything.
export interface FleetConfigOverride {
  name?: string;
  max_steps?: number;
  max_review_retries?: number;
  require_plan_approval?: boolean;
  always_full_crew?: boolean;
  entry_role?: FleetRole;
  roles?: Partial<Record<FleetRole, Partial<FleetRoleConfig>>>;
}

export type StreamEvent =
  | { type: "session.started"; data: { provider: Provider; model: string } }
  | { type: "assistant.text"; data: { text: string } }
  | { type: "assistant.tool_use"; data: { id: string; name: string; input: any } }
  | { type: "tool.result"; data: { tool_use_id: string; content: any; is_error: boolean } }
  | { type: "assistant.done"; data: { cost_usd?: number; duration_ms?: number } }
  | { type: "error"; data: { message: string } }
  // A vendor reported where its rate-limit window stands. The chat renders
  // nothing for it — the top bar's quota meter is what it feeds, via a refetch
  // of /api/system/quota on turn completion — but the union names it so an
  // exhaustive switch stays exhaustive.
  | {
      type: "quota.limit";
      data: {
        provider: Provider;
        status?: string | null;
        resets_at?: number | null;
        rate_limit_type?: string | null;
        utilization?: number | null;
      };
    }
  // Two gates share this event, and `kind` says which: "plan" is the fleet's
  // HITL pause after the planner (`plan` + `message`), "tool" is a permission
  // callback asking before one tool call (`tool` + `input` + `reason`). Both
  // are answered with the same `{type:"approval", id, value, feedback}` frame —
  // one approval UI for both vendors is the point.
  | {
      type: "pipeline.awaiting_approval";
      data: {
        id: string;
        kind: "plan" | "tool";
        timeout_s: number;
        /** plan gate */
        plan?: string;
        message?: string;
        /** tool gate */
        tool?: string;
        /** display-safe preview of the tool's arguments, already truncated */
        input?: Record<string, any>;
        reason?: string;
      };
    }
  // Synthesized by the backend's event bus for *this* viewer when its queue
  // overflowed: `dropped` events after `resume_from` never arrived. Unstamped
  // (no `_id`) — it is not part of the runner's replayable event stream.
  | { type: "stream.gap"; data: { dropped: number; resume_from: number } }
  | {
      type: "pipeline.approval_received";
      data: {
        id: string;
        value: "yes" | "no" | "timeout";
        feedback?: string | null;
        auto?: boolean;
      };
    };

/** Outbound WebSocket message shapes. The server distinguishes by `type` —
 * historical prompt frames omit `type` and are still accepted. */
export type WsClientMessage =
  | { prompt: string }
  | { type: "approval"; id: string; value: "yes" | "no"; feedback?: string };

export interface ChatBlock {
  kind: "text" | "tool_use" | "tool_result";
  text?: string;
  toolName?: string;
  toolInput?: any;
  toolUseId?: string;
  toolOutput?: any;
  isError?: boolean;
}

export interface ChatTurn {
  role: "user" | "assistant";
  blocks: ChatBlock[];
  costUsd?: number;
  durationMs?: number;
  inProgress?: boolean;
}

/** Stage status for a single fleet role within the current/last turn. */
export type RoleStatus = "idle" | "running" | "done" | "error";

/** One rolling window of one provider's plan, as GET /api/system/quota
 * reports it. `used`/`limit` are in `unit`: a vendor-reported window is a
 * fraction (utilization against a limit of 1.0), a locally-estimated one
 * counts tokens against no limit at all. */
export interface QuotaWindow {
  provider: string;
  key: string;
  window_s: number;
  started_at: number;
  used: number;
  limit: number | null;
  source: "provider" | "local";
  resets_at: number | null;
  unit: "tokens" | "fraction";
  headroom: number;
  confidence: "unknown" | "reported";
}

export interface QuotaProvider {
  /** 0-1, the MINIMUM across this provider's windows. */
  headroom: number;
  /** "unknown" means nothing measured it — do not draw a full bar as fact. */
  confidence: "unknown" | "reported";
  resets_at: number | null;
  windows: QuotaWindow[];
}

export interface QuotaSnapshot {
  generated_at: number;
  queue_threshold: number;
  providers: Record<string, QuotaProvider>;
  /** Every governed subscription is at or below the threshold. */
  queue_suggested: boolean;
}
