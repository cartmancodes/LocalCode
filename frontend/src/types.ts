export type Provider = "claude" | "opencode" | "fleet";

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
  upstream_id: string | null;
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
export type FleetRole = "planner" | "developer" | "coder" | "reviewer";

export interface FleetRoleConfig {
  provider: "claude" | "opencode";
  model: string;
  system_prompt: string;
}

export interface FleetConfig {
  name: string;
  planner: FleetRoleConfig;
  developer: FleetRoleConfig;
  coder: FleetRoleConfig;
  reviewer: FleetRoleConfig;
  max_steps: number;
  config_source: string | null;
}

export interface FleetConfigResponse {
  config: FleetConfig;
  is_default: boolean;
  valid_providers: ("claude" | "opencode")[];
  valid_roles: FleetRole[];
  defaults: FleetConfig;
}

// Partial dict of overrides — only the fields the user actually changed.
// Shape matches the YAML config schema.
export interface FleetConfigOverride {
  name?: string;
  max_steps?: number;
  roles?: Partial<Record<FleetRole, Partial<FleetRoleConfig>>>;
}

export interface Budget {
  spend_usd: number;
  daily_budget_usd: number;
  remaining_usd: number;
  window: string;
}

export type StreamEvent =
  | { type: "session.started"; data: { provider: Provider; model: string } }
  | { type: "assistant.text"; data: { text: string } }
  | { type: "assistant.tool_use"; data: { id: string; name: string; input: any } }
  | { type: "tool.result"; data: { tool_use_id: string; content: any; is_error: boolean } }
  | { type: "assistant.done"; data: { cost_usd?: number; duration_ms?: number } }
  | { type: "error"; data: { message: string } };

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
