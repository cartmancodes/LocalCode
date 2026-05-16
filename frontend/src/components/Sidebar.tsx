import { useMemo, useState } from "react";
import type { CatalogModel, SessionRow } from "../types";
import { IconCpu, IconPlus, IconSearch, IconTrash, IconX } from "./icons";

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
          <IconPlus size={13} />
          <span>New chat</span>
          <kbd className="lc-kbd">⌘N</kbd>
        </button>
        {/* Compact fleet/model picker for the next new chat. The big vertical
            models wall is gone — models otherwise live in fleet.yaml. */}
        <div className="lc-modelsel" title="Fleet / model for the next new chat">
          <IconCpu size={13} />
          <select
            value={p.pendingModelId}
            onChange={(e) => p.onPickModel(e.target.value)}
          >
            {p.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.provider}:{m.model}
              </option>
            ))}
          </select>
        </div>
        <div className="lc-search">
          <IconSearch size={13} />
          <input
            placeholder="Search chats…"
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

      <div className="lc-side__section lc-side__section--scroll">
        {p.sessions.length === 0 && (
          <div className="lc-side__hdr" style={{ justifyContent: "center" }}>
            No chats yet
          </div>
        )}
        {BUCKET_ORDER.map((bucket) =>
          grouped[bucket].length === 0 ? null : (
            <div key={bucket} className="lc-side__group">
              <div className="lc-side__hdr">
                <span>{bucket}</span>
                <span className="lc-side__hdr-act">{grouped[bucket].length}</span>
              </div>
              {grouped[bucket].map((s) => (
                <div
                  key={s.id}
                  className={`lc-chatrow ${s.id === p.activeId ? "is-active" : ""}`}
                  onClick={() => p.onSelect(s.id)}
                >
                  <span className="lc-chatrow__title">{s.title}</span>
                  <span className="lc-chatrow__meta">
                    <span className="lc-chatrow__fleet">
                      {s.provider}:{s.model}
                    </span>
                    <span className="lc-chatrow__sep" />
                    <span className="lc-chatrow__time">
                      {formatRelativeTime(s.updated_at)}
                    </span>
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
      </div>

      <div className="lc-side__foot">
        <div className="lc-user">
          <div className="lc-avatar">SK</div>
          <div className="lc-user__txt">
            <div className="lc-user__name">localhost</div>
            <div className="lc-user__sub">
              {p.sessions.length} chat{p.sessions.length === 1 ? "" : "s"}
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
