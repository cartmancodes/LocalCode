import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type {
  CatalogModel,
  FleetConfigOverride,
  FleetConfigResponse,
  FleetRole,
  FleetRoleConfig,
  WorkflowPreset,
} from "../types";
import { IconPlus, IconX } from "./icons";

interface Props {
  models: CatalogModel[];
  onCancel: () => void;
  onConfirm: (override: FleetConfigOverride | null) => void;
}

const ROLE_ORDER: FleetRole[] = ["planner", "developer", "coder", "reviewer"];
const ROLE_DESCRIPTIONS: Record<FleetRole, string> = {
  planner: "Decomposes the request into ordered steps. Outputs JSON.",
  developer: "Designs the approach for a step. No code.",
  coder: "Implements the step. May edit files / run bash.",
  reviewer: "Gates the previous step. LGTM or NACK.",
};

// Fallback role library — used if the backend's /api/fleet/config response
// somehow omits role_library (e.g. a stale or older backend). Each role MUST
// produce a valid RoleConfig so the modal can never crash on a missing key.
const ROLE_LIBRARY_FALLBACK: Record<FleetRole, FleetRoleConfig> = {
  planner:   { provider: "claude",   model: "claude-sonnet-4-6",    system_prompt: "" },
  developer: { provider: "claude",   model: "claude-opus-4-7",      system_prompt: "" },
  coder:     { provider: "opencode", model: "openai/gpt-5.3-codex", system_prompt: "" },
  reviewer:  { provider: "claude",   model: "claude-haiku-4-5",     system_prompt: "" },
};

function libRole(resp: FleetConfigResponse | null, role: FleetRole): FleetRoleConfig {
  return resp?.role_library?.[role] ?? ROLE_LIBRARY_FALLBACK[role];
}

/**
 * Workflow editor.
 *
 * The workflow IS its agents — only roles present in `draft.roles` are
 * part of the workflow. The user picks roles via preset chips or by toggling
 * individual role cards. When adding a previously-absent role we pre-fill
 * its config from the backend's `role_library` so the user doesn't have to
 * configure from scratch.
 */
export default function FleetConfigEditor({ models, onCancel, onConfirm }: Props) {
  const [resp, setResp] = useState<FleetConfigResponse | null>(null);
  const [draftRoles, setDraftRoles] = useState<Partial<Record<FleetRole, FleetRoleConfig>>>({});
  const [entryRole, setEntryRole] = useState<FleetRole>("coder");
  const [maxSteps, setMaxSteps] = useState<number>(6);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.fleetConfig();
        if (cancelled) return;
        // Defensive: bail to a clean error if the backend returned a malformed
        // shape rather than letting later destructures throw.
        if (!r?.config?.roles || typeof r.config.roles !== "object") {
          throw new Error(
            "fleet config endpoint returned an unexpected shape — restart the backend and reload"
          );
        }
        setResp(r);
        setDraftRoles({ ...r.config.roles });
        setEntryRole((r.config.entry_role || "coder") as FleetRole);
        setMaxSteps(r.config.max_steps || 6);
      } catch (e: any) {
        if (!cancelled) setError(String(e?.message ?? e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Group catalog models by underlying provider (drop fleet pseudo-entries).
  const modelsByProvider = useMemo(() => {
    const out: Record<string, string[]> = { claude: [], opencode: [] };
    for (const m of models) {
      if (m.provider === "claude" || m.provider === "opencode") {
        out[m.provider].push(m.model);
      }
    }
    return out;
  }, [models]);

  const presentRoles: FleetRole[] = useMemo(
    () => ROLE_ORDER.filter((r) => r in draftRoles),
    [draftRoles]
  );

  // Keep entry_role valid as the present roles change. MUST be declared before
  // any conditional return — Rules of Hooks. (Same goes for `activePresetKey`
  // below; all hooks live above the early-return wall.)
  useEffect(() => {
    if (presentRoles.length === 0) return;
    if (!presentRoles.includes(entryRole)) {
      const fallback =
        presentRoles.find((r) => r !== "planner") ?? presentRoles[0];
      setEntryRole(fallback);
    }
  }, [presentRoles, entryRole]);

  const activePresetKey = useMemo(() => {
    const presets = resp?.presets ?? {};
    const memberKey = presentRoles.slice().sort().join(",");
    for (const [key, p] of Object.entries(presets)) {
      if (p.roles.slice().sort().join(",") === memberKey) return key;
    }
    return null;
  }, [presentRoles, resp]);

  // ─── early returns (no hooks below this line) ──────────────────────────
  if (error) {
    return (
      <Modal onCancel={onCancel}>
        <h2>Configure Fleet</h2>
        <div className="muted">Failed to load fleet config: {error}</div>
        <Footer onCancel={onCancel} onConfirm={() => onConfirm(null)} confirmLabel="Use defaults" />
      </Modal>
    );
  }

  if (!resp) {
    return (
      <Modal onCancel={onCancel}>
        <h2>Configure Fleet</h2>
        <div className="muted">Loading…</div>
      </Modal>
    );
  }

  const applyPreset = (preset: WorkflowPreset) => {
    // Merge: keep existing role configs for roles that survive, pull from
    // role_library for roles being added. Roles dropped by the preset disappear.
    const next: Partial<Record<FleetRole, FleetRoleConfig>> = {};
    for (const role of preset.roles) {
      next[role] = draftRoles[role] ?? libRole(resp, role);
    }
    setDraftRoles(next);
    // Adjust entry_role if it's no longer present.
    setEntryRole(
      preset.roles.includes(entryRole) ? entryRole : (preset.entry_role as FleetRole)
    );
  };

  const toggleRole = (role: FleetRole) => {
    setDraftRoles((cur) => {
      const next = { ...cur };
      if (role in next) {
        // Removing — but never let the workflow become empty.
        const remaining = Object.keys(next).filter((k) => k !== role) as FleetRole[];
        if (remaining.length === 0) return cur;
        delete next[role];
      } else {
        next[role] = libRole(resp, role);
      }
      return next;
    });
  };

  const updateRole = (role: FleetRole, patch: Partial<FleetRoleConfig>) => {
    setDraftRoles((cur) => {
      const existing = cur[role];
      if (!existing) return cur;
      return { ...cur, [role]: { ...existing, ...patch } };
    });
  };

  // Build a partial override against the active config for a minimal diff.
  const buildOverride = (): FleetConfigOverride | null => {
    if (!resp) return null;
    const liveRoles = resp.config.roles ?? {};
    const sameMembership =
      Object.keys(liveRoles).sort().join(",") ===
      Object.keys(draftRoles).sort().join(",");

    const override: FleetConfigOverride = {};

    // Always send `roles` if membership changed; otherwise only if at least
    // one per-role field differs.
    let rolesDiffer = !sameMembership;
    if (sameMembership) {
      for (const role of presentRoles) {
        const live = liveRoles[role];
        const cur = draftRoles[role]!;
        if (
          live?.provider !== cur.provider ||
          live?.model !== cur.model ||
          live?.system_prompt !== cur.system_prompt
        ) {
          rolesDiffer = true;
          break;
        }
      }
    }
    if (rolesDiffer) {
      // When membership differs we must send the full new membership.
      // For each present role, only include diffed fields to keep the payload tight.
      const out: NonNullable<FleetConfigOverride["roles"]> = {};
      for (const role of presentRoles) {
        const cur = draftRoles[role]!;
        const live = liveRoles[role];
        if (!live || !sameMembership) {
          // New member or full replacement — send everything.
          out[role] = { ...cur };
        } else {
          const diff: Partial<FleetRoleConfig> = {};
          if (cur.provider !== live.provider) diff.provider = cur.provider;
          if (cur.model !== live.model) diff.model = cur.model;
          if (cur.system_prompt !== live.system_prompt) diff.system_prompt = cur.system_prompt;
          if (Object.keys(diff).length) out[role] = diff;
        }
      }
      override.roles = out;
    }
    if (entryRole !== resp.config.entry_role) override.entry_role = entryRole;
    if (maxSteps !== resp.config.max_steps) override.max_steps = maxSteps;
    return Object.keys(override).length ? override : null;
  };

  // (activePresetKey is computed above the early-return wall — see top of fn)

  const handleConfirm = () => onConfirm(buildOverride());
  const handleReset = () => {
    if (!resp) return;
    setDraftRoles({ ...(resp.config.roles ?? {}) });
    setEntryRole((resp.config.entry_role || "coder") as FleetRole);
    setMaxSteps(resp.config.max_steps || 6);
  };

  return (
    <Modal onCancel={onCancel}>
      <h2>Configure Fleet</h2>
      <div className="fleet-source muted">
        Source:{" "}
        {resp.is_default ? "built-in defaults" : resp.config.config_source ?? "(unknown)"}
      </div>

      {/* Preset chips ─────────────────────────────────────────────────── */}
      <div className="fleet-presets">
        <div className="fleet-section-hdr">Workflow</div>
        <div className="fleet-preset-row">
          {Object.entries(resp.presets ?? {}).map(([key, preset]) => (
            <button
              key={key}
              className={`fleet-preset ${activePresetKey === key ? "is-active" : ""}`}
              onClick={() => applyPreset(preset)}
              title={preset.description}
            >
              <span className="fleet-preset-label">{preset.label}</span>
              <span className="fleet-preset-roles">
                {preset.roles.map((r) => r[0].toUpperCase()).join(" → ")}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Role cards ──────────────────────────────────────────────────── */}
      <div className="fleet-section-hdr" style={{ marginTop: 14 }}>
        Agents <span className="muted" style={{ fontWeight: 400 }}>· toggle on / off; configure provider & model per agent</span>
      </div>
      <div className="fleet-roles">
        {ROLE_ORDER.map((role) => {
          const cur = draftRoles[role];
          const present = !!cur;
          const colors = ROLE_TINTS[role];
          return (
            <div
              key={role}
              className={`fleet-role-card ${present ? "is-on" : "is-off"}`}
              style={
                {
                  "--rc-fg": colors.fg,
                  "--rc-bg": colors.bg,
                  "--rc-bd": colors.bd,
                } as React.CSSProperties
              }
            >
              <div className="fleet-role-card__head">
                <button
                  className={`fleet-role-toggle ${present ? "is-on" : ""}`}
                  onClick={() => toggleRole(role)}
                  aria-label={present ? `Remove ${role}` : `Add ${role}`}
                  title={present ? "Remove from workflow" : "Add to workflow"}
                >
                  {present ? <IconX size={12} /> : <IconPlus size={12} />}
                </button>
                <div className="fleet-role-card__title">
                  <span className="fleet-role-card__name">{role}</span>
                  <span className="fleet-role-card__desc">{ROLE_DESCRIPTIONS[role]}</span>
                </div>
                {present && entryRole === role && (
                  <span className="fleet-role-card__entry" title="This role runs first">
                    entry
                  </span>
                )}
              </div>
              <div className="fleet-role-card__fields">
                <label>
                  <span className="muted">Provider</span>
                  <select
                    value={cur?.provider ?? libRole(resp, role).provider}
                    onChange={(e) => {
                      const p = e.target.value as "claude" | "opencode";
                      const next = (modelsByProvider[p] ?? [])[0] ?? cur?.model ?? "";
                      if (present) updateRole(role, { provider: p, model: next });
                      else
                        setDraftRoles((c) => ({
                          ...c,
                          [role]: { ...libRole(resp, role), provider: p, model: next },
                        }));
                    }}
                    disabled={!present}
                  >
                    <option value="claude">claude</option>
                    <option value="opencode">opencode</option>
                  </select>
                </label>
                <label>
                  <span className="muted">Model</span>
                  <select
                    value={cur?.model ?? libRole(resp, role).model}
                    onChange={(e) => updateRole(role, { model: e.target.value })}
                    disabled={!present}
                  >
                    {(() => {
                      const provider = (cur?.provider ?? libRole(resp, role).provider) as "claude" | "opencode";
                      const list = modelsByProvider[provider] ?? [];
                      const current = cur?.model ?? libRole(resp, role).model;
                      const opts = list.includes(current) ? list : [current, ...list];
                      return opts.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ));
                    })()}
                  </select>
                </label>
              </div>
            </div>
          );
        })}
      </div>

      {/* Entry-role picker (only relevant for single-agent workflows) ──── */}
      {(!presentRoles.includes("planner") || presentRoles.length === 1) && (
        <div className="fleet-meta">
          <label className="meta-row">
            <span>Entry role</span>
            <select
              value={entryRole}
              onChange={(e) => setEntryRole(e.target.value as FleetRole)}
              disabled={presentRoles.length === 0}
            >
              {presentRoles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <span className="muted" style={{ fontSize: 11 }}>
              runs first when there's no planner
            </span>
          </label>
        </div>
      )}

      <div className="fleet-meta">
        <label className="meta-row">
          <span>Max steps</span>
          <input
            type="number"
            min={1}
            max={12}
            value={maxSteps}
            onChange={(e) =>
              setMaxSteps(Math.max(1, Math.min(12, parseInt(e.target.value || "1", 10))))
            }
          />
        </label>
      </div>

      {/* Advanced — system prompt overrides ───────────────────────────── */}
      <button
        type="button"
        className="advanced-toggle muted"
        onClick={() => setShowAdvanced((s) => !s)}
      >
        {showAdvanced ? "▾" : "▸"} Advanced (system prompts)
      </button>
      {showAdvanced && (
        <div className="fleet-advanced">
          {presentRoles.map((role) => (
            <div key={role} className="advanced-role">
              <div className="advanced-role-name">{role}</div>
              <textarea
                rows={5}
                value={draftRoles[role]?.system_prompt ?? ""}
                onChange={(e) => updateRole(role, { system_prompt: e.target.value })}
              />
            </div>
          ))}
        </div>
      )}

      <Footer
        onCancel={onCancel}
        onConfirm={handleConfirm}
        onReset={handleReset}
        confirmLabel={
          presentRoles.length === 0 ? "Add at least one agent" : "Start chat"
        }
        confirmDisabled={presentRoles.length === 0}
      />
    </Modal>
  );
}

const ROLE_TINTS: Record<FleetRole, { fg: string; bg: string; bd: string }> = {
  planner:   { fg: "var(--ag-violet-fg)",  bg: "var(--ag-violet-bg)",  bd: "var(--ag-violet-bd)" },
  developer: { fg: "var(--ag-blue-fg)",    bg: "var(--ag-blue-bg)",    bd: "var(--ag-blue-bd)" },
  coder:     { fg: "var(--ag-emerald-fg)", bg: "var(--ag-emerald-bg)", bd: "var(--ag-emerald-bd)" },
  reviewer:  { fg: "var(--ag-amber-fg)",   bg: "var(--ag-amber-bg)",   bd: "var(--ag-amber-bd)" },
};

function Modal({ children, onCancel }: { children: React.ReactNode; onCancel: () => void }) {
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  );
}

function Footer({
  onCancel,
  onConfirm,
  onReset,
  confirmLabel,
  confirmDisabled,
}: {
  onCancel: () => void;
  onConfirm: () => void;
  onReset?: () => void;
  confirmLabel: string;
  confirmDisabled?: boolean;
}) {
  return (
    <div className="modal-footer">
      {onReset && (
        <button type="button" className="ghost" onClick={onReset}>
          Reset
        </button>
      )}
      <div className="spacer" />
      <button type="button" className="ghost" onClick={onCancel}>
        Cancel
      </button>
      <button
        type="button"
        className="primary"
        onClick={onConfirm}
        disabled={confirmDisabled}
      >
        {confirmLabel}
      </button>
    </div>
  );
}
