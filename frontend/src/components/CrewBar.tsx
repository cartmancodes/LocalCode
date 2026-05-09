import type { FleetConfig, FleetRole, SessionRow } from "../types";
import { IconCpu, IconRetry } from "./icons";

interface Props {
  fleet: FleetConfig;
  session: SessionRow;
  activeRole: FleetRole | null;
  onPickRole: (role: FleetRole | null) => void;
  onConfigure?: () => void;
}

const ROLES: FleetRole[] = ["planner", "developer", "coder", "reviewer"];

const ROLE_COLORS: Record<FleetRole, { fg: string; bg: string; bd: string }> = {
  planner:   { fg: "var(--ag-violet-fg)",  bg: "var(--ag-violet-bg)",  bd: "var(--ag-violet-bd)" },
  developer: { fg: "var(--ag-blue-fg)",    bg: "var(--ag-blue-bg)",    bd: "var(--ag-blue-bd)" },
  coder:     { fg: "var(--ag-emerald-fg)", bg: "var(--ag-emerald-bg)", bd: "var(--ag-emerald-bd)" },
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
 * Shown above the chat stream for fleet sessions only — surfaces each role's
 * configured provider+model and (eventually) live token / cost stats. Click a
 * card to filter the stream to that role's messages.
 */
export default function CrewBar({ fleet, session, activeRole, onPickRole, onConfigure }: Props) {
  return (
    <div className="lc-crew">
      <div className="lc-crew__head">
        <div className="lc-crew__title">
          <span className="lc-crew__eyebrow">Crew</span>
          <h1 className="lc-crew__name">{fleet.name}</h1>
          <span className="lc-crew__sub">{ROLES.length} agents</span>
        </div>
        <div className="lc-crew__tools">
          <button className="lc-ghostbtn" title="Retry turn (not yet wired)">
            <IconRetry size={14} /> Retry turn
          </button>
          {onConfigure && (
            <button className="lc-ghostbtn" onClick={onConfigure} title="Configure roles">
              <IconCpu size={14} /> Configure
            </button>
          )}
        </div>
      </div>
      <div className="lc-crew__row">
        {ROLES.map((role) => {
          const r = fleet[role];
          const colors = ROLE_COLORS[role];
          const isActive = activeRole === role;
          const overridden = isRoleOverridden(role, session.fleet_config_override);
          return (
            <button
              key={role}
              className={`lc-agent ${isActive ? "is-active" : ""}`}
              onClick={() => onPickRole(isActive ? null : role)}
              style={
                {
                  "--agc-fg": colors.fg,
                  "--agc-bg": colors.bg,
                  "--agc-bd": colors.bd,
                } as React.CSSProperties
              }
              title={`${role} — ${r.provider}:${r.model}${overridden ? " (UI override)" : ""}`}
            >
              <span className="lc-agent__avatar">{role.charAt(0).toUpperCase()}</span>
              <span className="lc-agent__txt">
                <span className="lc-agent__role">
                  {role}
                  {overridden && <span className="lc-agent__overridden" title="UI override">●</span>}
                </span>
                <span className="lc-agent__model">
                  {r.provider}
                  <span className="lc-agent__sep">·</span>
                  {r.model}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
