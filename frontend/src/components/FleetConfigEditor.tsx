import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type {
  CatalogModel,
  FleetConfig,
  FleetConfigOverride,
  FleetConfigResponse,
  FleetRole,
  FleetRoleConfig,
} from "../types";

interface Props {
  models: CatalogModel[]; // global catalog; we filter to non-fleet entries by provider
  onCancel: () => void;
  onConfirm: (override: FleetConfigOverride | null) => void;
}

const ROLES: FleetRole[] = ["planner", "developer", "coder", "reviewer"];

/**
 * Modal-style editor that loads the current fleet config and lets the user
 * override any role's provider/model (and, under "Advanced", system_prompt)
 * before starting a new chat session. Emits a partial override dict — only
 * the fields the user actually changed are included, so omitted fields keep
 * inheriting the file-level / built-in defaults.
 */
export default function FleetConfigEditor({ models, onCancel, onConfirm }: Props) {
  const [resp, setResp] = useState<FleetConfigResponse | null>(null);
  const [draft, setDraft] = useState<Record<FleetRole, FleetRoleConfig> | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [maxSteps, setMaxSteps] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.fleetConfig();
        if (cancelled) return;
        setResp(r);
        setDraft({
          planner: { ...r.config.planner },
          developer: { ...r.config.developer },
          coder: { ...r.config.coder },
          reviewer: { ...r.config.reviewer },
        });
        setMaxSteps(r.config.max_steps);
      } catch (e: any) {
        if (!cancelled) setError(String(e?.message ?? e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Group catalog models by underlying provider, dropping the fleet pseudo-entries.
  const modelsByProvider = useMemo(() => {
    const out: Record<string, string[]> = { claude: [], opencode: [] };
    for (const m of models) {
      if (m.provider === "claude" || m.provider === "opencode") {
        out[m.provider].push(m.model);
      }
    }
    return out;
  }, [models]);

  if (error) {
    return (
      <Modal onCancel={onCancel}>
        <h2>Configure Fleet</h2>
        <div className="muted">Failed to load fleet config: {error}</div>
        <Footer onCancel={onCancel} onConfirm={() => onConfirm(null)} confirmLabel="Use defaults" />
      </Modal>
    );
  }

  if (!resp || !draft || maxSteps == null) {
    return (
      <Modal onCancel={onCancel}>
        <h2>Configure Fleet</h2>
        <div className="muted">Loading…</div>
      </Modal>
    );
  }

  const defaults = resp.config; // the active config — we diff against this

  const updateRole = (role: FleetRole, patch: Partial<FleetRoleConfig>) => {
    setDraft((cur) => (cur ? { ...cur, [role]: { ...cur[role], ...patch } } : cur));
  };

  // Build a partial override dict containing only fields that differ from the
  // active config. Empty roles object = no override needed.
  const buildOverride = (): FleetConfigOverride | null => {
    const roles: NonNullable<FleetConfigOverride["roles"]> = {};
    for (const role of ROLES) {
      const cur = draft[role];
      const def = defaults[role];
      const diff: Partial<FleetRoleConfig> = {};
      if (cur.provider !== def.provider) diff.provider = cur.provider;
      if (cur.model !== def.model) diff.model = cur.model;
      if (cur.system_prompt !== def.system_prompt) diff.system_prompt = cur.system_prompt;
      if (Object.keys(diff).length) roles[role] = diff;
    }
    const override: FleetConfigOverride = {};
    if (Object.keys(roles).length) override.roles = roles;
    if (maxSteps !== defaults.max_steps) override.max_steps = maxSteps;
    return Object.keys(override).length ? override : null;
  };

  const handleConfirm = () => onConfirm(buildOverride());
  const handleReset = () => {
    setDraft({
      planner: { ...defaults.planner },
      developer: { ...defaults.developer },
      coder: { ...defaults.coder },
      reviewer: { ...defaults.reviewer },
    });
    setMaxSteps(defaults.max_steps);
  };

  return (
    <Modal onCancel={onCancel}>
      <h2>Configure Fleet</h2>
      <div className="fleet-source muted">
        Source:{" "}
        {resp.is_default
          ? "built-in defaults"
          : resp.config.config_source ?? "(unknown)"}
      </div>

      <div className="fleet-roles">
        {ROLES.map((role) => (
          <RoleRow
            key={role}
            role={role}
            value={draft[role]}
            modelsByProvider={modelsByProvider}
            onChange={(patch) => updateRole(role, patch)}
            isOverridden={
              draft[role].provider !== defaults[role].provider ||
              draft[role].model !== defaults[role].model ||
              draft[role].system_prompt !== defaults[role].system_prompt
            }
          />
        ))}
      </div>

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

      <button
        type="button"
        className="advanced-toggle muted"
        onClick={() => setShowAdvanced((s) => !s)}
      >
        {showAdvanced ? "▾" : "▸"} Advanced (system prompts)
      </button>
      {showAdvanced && (
        <div className="fleet-advanced">
          {ROLES.map((role) => (
            <div key={role} className="advanced-role">
              <div className="advanced-role-name">{role}</div>
              <textarea
                rows={5}
                value={draft[role].system_prompt}
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
        confirmLabel="Start chat"
      />
    </Modal>
  );
}

function RoleRow({
  role,
  value,
  modelsByProvider,
  onChange,
  isOverridden,
}: {
  role: FleetRole;
  value: FleetRoleConfig;
  modelsByProvider: Record<string, string[]>;
  onChange: (patch: Partial<FleetRoleConfig>) => void;
  isOverridden: boolean;
}) {
  const models = modelsByProvider[value.provider] ?? [];
  // Always include the current model in the dropdown even if it isn't in the
  // catalog — so an unconventional model from the YAML stays selectable.
  const modelOptions = models.includes(value.model) ? models : [value.model, ...models];
  return (
    <div className={`fleet-role ${isOverridden ? "overridden" : ""}`}>
      <div className="fleet-role-name">{role}</div>
      <div className="fleet-role-fields">
        <label>
          <span className="muted">Provider</span>
          <select
            value={value.provider}
            onChange={(e) => {
              const p = e.target.value as "claude" | "opencode";
              // Switching provider invalidates the model — pick the first option for the new provider.
              const next = (modelsByProvider[p] ?? [value.model])[0] ?? value.model;
              onChange({ provider: p, model: next });
            }}
          >
            <option value="claude">claude</option>
            <option value="opencode">opencode</option>
          </select>
        </label>
        <label>
          <span className="muted">Model</span>
          <select
            value={value.model}
            onChange={(e) => onChange({ model: e.target.value })}
          >
            {modelOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

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
}: {
  onCancel: () => void;
  onConfirm: () => void;
  onReset?: () => void;
  confirmLabel: string;
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
      <button type="button" className="primary" onClick={onConfirm}>
        {confirmLabel}
      </button>
    </div>
  );
}

// Type helper — re-export FleetConfig to silence unused-import warnings
export type { FleetConfig };
