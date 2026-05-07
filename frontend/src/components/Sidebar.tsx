import type { CatalogModel, SessionRow } from "../types";
import ModelPicker from "./ModelPicker";

interface Props {
  sessions: SessionRow[];
  activeId: string | null;
  models: CatalogModel[];
  pendingModelId: string;
  onPickModel: (id: string) => void;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
}

export default function Sidebar(p: Props) {
  return (
    <aside className="sidebar">
      <div className="brand">LocalCode</div>

      <div className="new-chat">
        <ModelPicker models={p.models} value={p.pendingModelId} onChange={p.onPickModel} />
        <button onClick={p.onCreate} className="primary">+ New chat</button>
      </div>

      <div className="session-list">
        {p.sessions.length === 0 && <div className="muted">No chats yet.</div>}
        {p.sessions.map((s) => (
          <div
            key={s.id}
            className={`session-row ${p.activeId === s.id ? "active" : ""}`}
            onClick={() => p.onSelect(s.id)}
          >
            <div className="session-title">{s.title}</div>
            <div className="session-meta">
              <span className={`badge ${s.provider}`}>{s.provider}</span>
              <span className="muted">{s.model}</span>
            </div>
            <button
              className="delete"
              title="Delete"
              onClick={(e) => {
                e.stopPropagation();
                p.onDelete(s.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </aside>
  );
}
