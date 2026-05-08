import type { CatalogModel } from "../types";

interface Props {
  models: CatalogModel[];
  value: string; // catalog id, e.g. "claude:claude-sonnet-4-6"
  onChange: (id: string) => void;
}

export default function ModelPicker({ models, value, onChange }: Props) {
  const grouped = models.reduce<Record<string, CatalogModel[]>>((acc, m) => {
    (acc[m.provider] ??= []).push(m);
    return acc;
  }, {});
  return (
    <select
      className="model-picker"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      {Object.entries(grouped).map(([provider, list]) => (
        <optgroup
          key={provider}
          label={
            provider === "claude"
              ? "Claude Code"
              : provider === "opencode"
              ? "OpenCode"
              : "Fleet (multi-agent)"
          }
        >
          {list.map((m) => (
            <option key={m.id} value={m.id}>
              {m.model}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
