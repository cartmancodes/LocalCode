import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import ChatPane from "./components/ChatPane";
import FleetConfigEditor from "./components/FleetConfigEditor";
import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import type { CatalogModel, FleetConfigOverride, SessionRow } from "./types";

type Theme = "light" | "dark";
type Accent = "clay" | "violet" | "blue";

const THEME_KEY = "lc-theme";
const ACCENT_KEY = "lc-accent";

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(THEME_KEY) as Theme | null;
    return saved === "light" || saved === "dark" ? saved : "dark";
  });
  const [accent, setAccent] = useState<Accent>(() => {
    const saved = localStorage.getItem(ACCENT_KEY) as Accent | null;
    return saved && ["clay", "violet", "blue"].includes(saved) ? saved : "clay";
  });
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const [models, setModels] = useState<CatalogModel[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [pendingModelId, setPendingModelId] = useState<string>("");
  const [fleetEditorOpen, setFleetEditorOpen] = useState(false);

  // Apply theme/accent to <html> data attrs.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  useEffect(() => {
    document.documentElement.dataset.accent = accent;
    localStorage.setItem(ACCENT_KEY, accent);
  }, [accent]);

  // Load catalog + sessions on boot.
  useEffect(() => {
    (async () => {
      const [m, s] = await Promise.all([api.listModels(), api.listSessions()]);
      setModels(m);
      setSessions(s);
      if (m.length && !pendingModelId) setPendingModelId(m[0].id);
      if (s.length && !activeId) setActiveId(s[0].id);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ⌘N → new chat.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        onCreate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingModelId]);

  const active = useMemo(
    () => sessions.find((s) => s.id === activeId) ?? null,
    [sessions, activeId]
  );

  const createWithOverride = async (override: FleetConfigOverride | null) => {
    if (!pendingModelId) return;
    const [provider, model] = pendingModelId.split(":") as [
      SessionRow["provider"],
      string,
    ];
    const fresh = await api.createSession({
      provider,
      model,
      fleet_config_override: provider === "fleet" ? override : null,
    });
    setSessions((cur) => [fresh, ...cur]);
    setActiveId(fresh.id);
  };

  const onCreate = async () => {
    if (!pendingModelId) return;
    const [provider] = pendingModelId.split(":");
    if (provider === "fleet") {
      setFleetEditorOpen(true);
      return;
    }
    await createWithOverride(null);
  };

  const onDelete = async (id: string) => {
    await api.deleteSession(id);
    setSessions((cur) => cur.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  };

  const onClearAll = async () => {
    await api.deleteAllSessions();
    setSessions([]);
    setActiveId(null);
  };

  const cycleAccent = () => {
    const order: Accent[] = ["clay", "violet", "blue"];
    setAccent(order[(order.indexOf(accent) + 1) % order.length]);
  };

  return (
    <div className="lc-root">
      <Topbar
        session={active}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        onToggleSidebar={() => setSidebarOpen((o) => !o)}
        onNewChat={onCreate}
      />
      <div className={`lc-shell ${sidebarOpen ? "" : "lc-shell--no-side"}`}>
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          models={models}
          pendingModelId={pendingModelId}
          onPickModel={setPendingModelId}
          onSelect={setActiveId}
          onCreate={onCreate}
          onDelete={onDelete}
          onClearAll={onClearAll}
        />
        <ChatPane
          session={active}
          onConfigureFleet={
            active?.provider === "fleet"
              ? () => setFleetEditorOpen(true)
              : undefined
          }
        />
      </div>

      {fleetEditorOpen && (
        <FleetConfigEditor
          models={models}
          onCancel={() => setFleetEditorOpen(false)}
          onConfirm={async (override) => {
            setFleetEditorOpen(false);
            await createWithOverride(override);
          }}
        />
      )}

      {/* Floating accent toggle — small power-user control, top-right corner */}
      <button
        onClick={cycleAccent}
        title={`Accent: ${accent} (click to cycle)`}
        style={{
          position: "fixed",
          right: 16,
          bottom: 16,
          width: 32,
          height: 32,
          borderRadius: "50%",
          background: "var(--accent)",
          color: "#fff",
          border: "0",
          boxShadow: "var(--shadow-sm)",
          fontSize: 11,
          fontFamily: "Geist Mono, monospace",
          cursor: "pointer",
          opacity: 0.55,
        }}
      >
        {accent.charAt(0).toUpperCase()}
      </button>
    </div>
  );
}
