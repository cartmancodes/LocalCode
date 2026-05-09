import { useMemo, useState } from "react";
import type { CatalogModel, SessionRow } from "../types";
import { IconPlus, IconSearch, IconTrash, IconX } from "./icons";

interface Props {
  sessions: SessionRow[];
  activeId: string | null;
  models: CatalogModel[];
  pendingModelId: string;
  onPickModel: (id: string) => void;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onClearAll: () => void;
}

function formatRelativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  const now = Date.now();
  const mins = Math.floor((now - t) / 60_000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) {
    return new Date(t).toLocaleDateString(undefined, { weekday: "short" });
  }
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function bucketFor(iso: string): "Today" | "Yesterday" | "This week" | "Earlier" {
  const t = new Date(iso).getTime();
  const now = Date.now();
  const dayMs = 24 * 60 * 60 * 1000;
  const days = Math.floor((now - t) / dayMs);
  if (days < 1) return "Today";
  if (days < 2) return "Yesterday";
  if (days < 7) return "This week";
  return "Earlier";
}

const BUCKET_ORDER: Array<ReturnType<typeof bucketFor>> = [
  "Today",
  "Yesterday",
  "This week",
  "Earlier",
];

export default function Sidebar(p: Props) {
  const [q, setQ] = useState("");

  const grouped = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const filtered = ql
      ? p.sessions.filter(
          (s) =>
            s.title.toLowerCase().includes(ql) ||
            s.provider.toLowerCase().includes(ql) ||
            s.model.toLowerCase().includes(ql)
        )
      : p.sessions;
    const buckets: Record<string, SessionRow[]> = {
      Today: [],
      Yesterday: [],
      "This week": [],
      Earlier: [],
    };
    filtered.forEach((s) => buckets[bucketFor(s.updated_at)].push(s));
    return buckets;
  }, [p.sessions, q]);

  return (
    <aside className="lc-sidebar">
      <div className="lc-side__top">
        <button className="lc-newchat" onClick={p.onCreate}>
          <IconPlus size={14} />
          <span>New chat</span>
          <kbd className="lc-kbd">⌘N</kbd>
        </button>
        <div className="lc-search">
          <IconSearch size={14} />
          <input
            placeholder="Search chats"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {q && (
            <button className="lc-search__clear" onClick={() => setQ("")}>
              <IconX size={12} />
            </button>
          )}
        </div>
      </div>

      <div className="lc-side__section">
        <div className="lc-side__hdr">Models</div>
        <div className="lc-modelpicker">
          {p.models.map((m) => (
            <button
              key={m.id}
              className={`lc-model ${p.pendingModelId === m.id ? "is-active" : ""}`}
              onClick={() => p.onPickModel(m.id)}
              title={m.id}
            >
              <span className="lc-model__name">{m.model}</span>
              <span className="lc-model__meta">{m.provider}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="lc-side__section lc-side__section--scroll">
        {p.sessions.length > 0 && (
          <button
            className="lc-clear-all"
            onClick={() => {
              if (confirm(`Delete all ${p.sessions.length} chats? Cannot be undone.`)) {
                p.onClearAll();
              }
            }}
          >
            Clear all chats
          </button>
        )}
        {p.sessions.length === 0 && (
          <div className="lc-side__hdr" style={{ textAlign: "center" }}>
            No chats yet
          </div>
        )}
        {BUCKET_ORDER.map((bucket) =>
          grouped[bucket].length === 0 ? null : (
            <div key={bucket} className="lc-side__group">
              <div className="lc-side__hdr">{bucket}</div>
              {grouped[bucket].map((s) => (
                <div
                  key={s.id}
                  className={`lc-chatrow ${s.id === p.activeId ? "is-active" : ""}`}
                  onClick={() => p.onSelect(s.id)}
                >
                  <span className="lc-chatrow__dot" />
                  <span className="lc-chatrow__title">{s.title}</span>
                  <span className="lc-chatrow__fleet">
                    {s.provider}:{s.model}
                  </span>
                  <span className="lc-chatrow__time">
                    {formatRelativeTime(s.updated_at)}
                  </span>
                  <button
                    className="lc-chatrow__del"
                    title="Delete chat"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirm("Delete this chat?")) p.onDelete(s.id);
                    }}
                  >
                    <IconTrash size={12} />
                  </button>
                </div>
              ))}
            </div>
          )
        )}
      </div>

      <div className="lc-side__foot">
        <div className="lc-user">
          <div className="lc-avatar">SK</div>
          <div className="lc-user__txt">
            <div className="lc-user__name">localhost</div>
            <div className="lc-user__sub">{p.sessions.length} chats</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
