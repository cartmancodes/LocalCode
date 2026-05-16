import React from "react";
import type { FleetConfig, FleetRole, RoleStatus, SessionRow } from "../types";
import { IconCheck, IconCpu, IconRetry, IconStop, IconX } from "./icons";

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
 * Slim inline workflow strip (Panel A · "Inline / Calm"). The pipeline is a
 * tight P→C→R row of role nodes joined by connectors; below it a single
 * current-step line. Click a node to filter the transcript to that agent.
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

  const total = presentRoles.length;
  const doneCount = presentRoles.filter((r) => roleStatuses[r] === "done").length;
  const errorCount = presentRoles.filter((r) => roleStatuses[r] === "error").length;
  const runningRole = presentRoles.find((r) => roleStatuses[r] === "running");
  let progressLabel: string;
  let progressTone: "idle" | "running" | "done" | "error";
  if (runningRole) {
    progressLabel = `running · ${runningRole}`;
    progressTone = "running";
  } else if (doneCount + errorCount === 0) {
    progressLabel = "ready";
    progressTone = "idle";
  } else if (errorCount > 0) {
    progressLabel = `${doneCount}/${total} · ${errorCount} error${errorCount === 1 ? "" : "s"}`;
    progressTone = "error";
  } else if (doneCount === total) {
    progressLabel = "all done";
    progressTone = "done";
  } else {
    progressLabel = `${doneCount}/${total} done`;
    progressTone = "done";
  }

  // A connector lights up once the node to its left has progressed
  // (done or currently running) — mirrors the prototype's active P→C link.
  const litUpTo = (() => {
    let idx = -1;
    presentRoles.forEach((r, i) => {
      const st = roleStatuses[r];
      if (st === "done" || st === "running") idx = i;
    });
    return idx;
  })();

  return (
    <div className="lc-crew">
      <div className="lc-crew__head">
        <div className="lc-crew__title">
          <span className="lc-crew__eyebrow">Workflow</span>
          <h1 className="lc-crew__name">{fleet.name}</h1>
          <span className="lc-crew__sub">
            {presentRoles.length} agent{presentRoles.length === 1 ? "" : "s"}
            {session.additional_dirs && session.additional_dirs.length > 0
              ? ` · +${session.additional_dirs.length} dir${session.additional_dirs.length === 1 ? "" : "s"}`
              : ""}
          </span>
        </div>
        <div className="lc-crew__tools">
          <span
            className={`lc-progress lc-progress--${progressTone}`}
            title={`Pipeline — ${doneCount}/${total} done${errorCount ? `, ${errorCount} error${errorCount === 1 ? "" : "s"}` : ""}`}
          >
            <span className={`lc-progress__dot lc-progress__dot--${progressTone}`} />
            <span>{progressLabel}</span>
          </span>
          {onConfigure && (
            <button className="lc-ghostbtn" onClick={onConfigure} title="Configure agents">
              <IconCpu size={13} />
            </button>
          )}
          <button className="lc-ghostbtn" title={runningRole ? "Stop run" : "Retry turn"}>
            {runningRole ? <IconStop size={13} /> : <IconRetry size={13} />}
          </button>
        </div>
      </div>

      <div className="lc-crew__row">
        {presentRoles.map((role, i) => {
          const r = fleet.roles[role]!;
          const colors = ROLE_COLORS[role];
          const isActive = activeRole === role;
          const overridden = isRoleOverridden(role, session.fleet_config_override);
          const status: RoleStatus = roleStatuses[role] ?? "idle";
          return (
            <React.Fragment key={role}>
              {i > 0 && (
                <span className={`lc-pipe-conn ${i - 1 <= litUpTo ? "is-active" : ""}`} />
              )}
              <button
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
                  <span className="lc-agent__model">{r.model}</span>
                </span>
                <RoleStatusBadge status={status} />
              </button>
            </React.Fragment>
          );
        })}
      </div>

      {runningRole && (
        <div className="lc-curstep">
          <RoleBadge role={runningRole} />
          <span style={{ color: "var(--ink)" }}>working</span>
          <span className="lc-curstep__meta">
            · {doneCount + 1} / {total}
          </span>
        </div>
      )}
    </div>
  );
}

function RoleBadge({ role }: { role: FleetRole }) {
  const c = ROLE_COLORS[role];
  return (
    <span
      className="role-badge"
      style={
        { "--agc-fg": c.fg, "--agc-bg": c.bg, "--agc-bd": c.bd } as React.CSSProperties
      }
    >
      <span className="role-dot" />
      {role.slice(0, 3).toUpperCase()}
    </span>
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
