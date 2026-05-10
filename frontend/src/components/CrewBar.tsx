import type { FleetConfig, FleetRole, RoleStatus, SessionRow } from "../types";
import { IconCheck, IconCpu, IconRetry, IconX } from "./icons";

interface Props {
  fleet: FleetConfig;
  session: SessionRow;
  activeRole: FleetRole | null;
  onPickRole: (role: FleetRole | null) => void;
  onConfigure?: () => void;
  /** Per-role pipeline status for the latest turn. Missing roles are idle. */
  roleStatuses?: Partial<Record<FleetRole, RoleStatus>>;
}

const ROLE_ORDER: FleetRole[] = ["planner", "developer", "coder", "reviewer", "tester"];

const ROLE_COLORS: Record<FleetRole, { fg: string; bg: string; bd: string }> = {
  planner:   { fg: "var(--ag-violet-fg)",  bg: "var(--ag-violet-bg)",  bd: "var(--ag-violet-bd)" },
  developer: { fg: "var(--ag-blue-fg)",    bg: "var(--ag-blue-bg)",    bd: "var(--ag-blue-bd)" },
  coder:     { fg: "var(--ag-emerald-fg)", bg: "var(--ag-emerald-bg)", bd: "var(--ag-emerald-bd)" },
  tester:    { fg: "var(--ag-rose-fg)",    bg: "var(--ag-rose-bg)",    bd: "var(--ag-rose-bd)" },
  reviewer:  { fg: "var(--ag-amber-fg)",   bg: "var(--ag-amber-bg)",   bd: "var(--ag-amber-bd)" },
};

function isRoleOverridden(
  role: FleetRole,
  override: SessionRow["fleet_config_override"]
): boolean {
  if (!override?.roles?.[role]) return false;
  const o = override.roles[role]!;
  return Boolean(o.provider || o.model || o.system_prompt);
}

/**
 * Renders the agents that are part of THIS workflow — only roles present in
 * `fleet.roles` are shown. Click a card to filter the stream to that agent's
 * messages.
 */
export default function CrewBar({
  fleet,
  session,
  activeRole,
  onPickRole,
  onConfigure,
  roleStatuses = {},
}: Props) {
  const presentRoles = ROLE_ORDER.filter((r) => fleet.roles[r] != null);

  // Progress headline: "X of Y done", or the role currently running, or
  // "ready" between turns. Cheap derivation, lives next to the bar so it
  // gives users a single glanceable status without reading every card.
  const total = presentRoles.length;
  const doneCount = presentRoles.filter((r) => roleStatuses[r] === "done").length;
  const errorCount = presentRoles.filter((r) => roleStatuses[r] === "error").length;
  const runningRole = presentRoles.find((r) => roleStatuses[r] === "running");
  let progressLabel: string;
  let progressTone: "idle" | "running" | "done" | "error";
  if (runningRole) {
    progressLabel = `Running · ${runningRole}`;
    progressTone = "running";
  } else if (doneCount + errorCount === 0) {
    progressLabel = "Ready";
    progressTone = "idle";
  } else if (errorCount > 0) {
    progressLabel = `${doneCount}/${total} done · ${errorCount} error${errorCount === 1 ? "" : "s"}`;
    progressTone = "error";
  } else if (doneCount === total) {
    progressLabel = "All done";
    progressTone = "done";
  } else {
    progressLabel = `${doneCount}/${total} done`;
    progressTone = "done";
  }

  return (
    <div className="lc-crew">
      <div className="lc-crew__head">
        <div className="lc-crew__title">
          <span className="lc-crew__eyebrow">Workflow</span>
          <h1 className="lc-crew__name">{fleet.name}</h1>
          <span className="lc-crew__sub">
            {presentRoles.length} agent{presentRoles.length === 1 ? "" : "s"}
            {fleet.entry_role && (!fleet.roles.planner || presentRoles.length === 1)
              ? ` · entry: ${fleet.entry_role}`
              : ""}
            {session.cwd
              ? ` · cwd: ${session.cwd.replace(/^\/Users\/[^/]+/, "~")}`
              : ""}
            {session.additional_dirs && session.additional_dirs.length > 0
              ? ` · +${session.additional_dirs.length} dir${session.additional_dirs.length === 1 ? "" : "s"}`
              : ""}
          </span>
        </div>
        <div className="lc-crew__tools">
          <span
            className={`lc-progress lc-progress--${progressTone}`}
            title={`Pipeline status — ${doneCount}/${total} done${errorCount ? `, ${errorCount} error${errorCount === 1 ? "" : "s"}` : ""}`}
          >
            <span className={`lc-progress__dot lc-progress__dot--${progressTone}`} />
            <span className="lc-progress__lbl">{progressLabel}</span>
          </span>
          <button className="lc-ghostbtn" title="Retry turn (not yet wired)">
            <IconRetry size={14} /> Retry turn
          </button>
          {onConfigure && (
            <button className="lc-ghostbtn" onClick={onConfigure} title="Configure agents">
              <IconCpu size={14} /> Configure
            </button>
          )}
        </div>
      </div>
      <div
        className="lc-crew__row"
        style={{
          gridTemplateColumns: `repeat(${Math.max(1, presentRoles.length)}, 1fr)`,
        }}
      >
        {presentRoles.map((role) => {
          const r = fleet.roles[role]!;
          const colors = ROLE_COLORS[role];
          const isActive = activeRole === role;
          const overridden = isRoleOverridden(role, session.fleet_config_override);
          const status: RoleStatus = roleStatuses[role] ?? "idle";
          return (
            <button
              key={role}
              className={`lc-agent lc-agent--${status} ${isActive ? "is-active" : ""}`}
              onClick={() => onPickRole(isActive ? null : role)}
              style={
                {
                  "--agc-fg": colors.fg,
                  "--agc-bg": colors.bg,
                  "--agc-bd": colors.bd,
                } as React.CSSProperties
              }
              title={`${role} — ${r.provider}:${r.model}${overridden ? " (UI override)" : ""} · ${status}`}
            >
              <span className="lc-agent__avatar">{role.charAt(0).toUpperCase()}</span>
              <span className="lc-agent__txt">
                <span className="lc-agent__role">
                  {role}
                  {overridden && (
                    <span className="lc-agent__overridden" title="UI override">●</span>
                  )}
                </span>
                <span className="lc-agent__model">
                  {r.provider}
                  <span className="lc-agent__sep">·</span>
                  {r.model}
                </span>
              </span>
              <RoleStatusBadge status={status} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function RoleStatusBadge({ status }: { status: RoleStatus }) {
  if (status === "running") {
    return <span className="lc-agent__status lc-agent__status--running" aria-label="running" />;
  }
  if (status === "done") {
    return (
      <span className="lc-agent__status lc-agent__status--done" aria-label="done">
        <IconCheck size={12} />
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="lc-agent__status lc-agent__status--error" aria-label="error">
        <IconX size={12} />
      </span>
    );
  }
  return <span className="lc-agent__status lc-agent__status--idle" aria-hidden="true" />;
}
