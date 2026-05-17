import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import ChatPane from "./components/ChatPane";
import ErrorBoundary from "./components/ErrorBoundary";
import FleetConfigEditor from "./components/FleetConfigEditor";
import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import type {
  CatalogModel,
  FleetConfigOverride,
  PermissionMode,
  SessionRow,
} from "./types";

type Theme = "light" | "dark";
type Accent = "clay" | "violet" | "blue";

const THEME_KEY = "lc-theme";
const ACCENT_KEY = "lc-accent";
const CWD_KEY = "lc-cwd";          // user's chosen project root for new chats; null = use backend default
const ADD_DIRS_KEY = "lc-add-dirs"; // user's additional-dirs grant list for new chats (JSON-encoded array)
const PERM_KEY = "lc-permission-mode"; // permission/auto mode for new chats

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
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(() => {
    const saved = localStorage.getItem(PERM_KEY) as PermissionMode | null;
    return saved &&
      ["acceptEdits", "default", "plan", "bypassPermissions"].includes(saved)
      ? saved
      : "acceptEdits";
  });

  // Working directory + additional-dirs for newly-created chats.
  // - `cwd` (state): user's explicit override, persisted to localStorage. null = follow defaultCwd.
  // - `defaultCwd`: orchestrator process's cwd, fetched once at boot.
  // - `additionalDirs`: extra absolute paths to grant the agent's tools, on top of cwd.
  const [cwd, setCwd] = useState<string | null>(() => localStorage.getItem(CWD_KEY));
  const [defaultCwd, setDefaultCwd] = useState<string | null>(null);
  const [additionalDirs, setAdditionalDirs] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(ADD_DIRS_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((s) => typeof s === "string") : [];
    } catch {
      return [];
    }
  });
  const effectiveCwd = cwd ?? defaultCwd;

  useEffect(() => {
    localStorage.setItem(PERM_KEY, permissionMode);
  }, [permissionMode]);

  // Apply theme/accent to <html> data attrs.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  useEffect(() => {
    document.documentElement.dataset.accent = accent;
    localStorage.setItem(ACCENT_KEY, accent);
  }, [accent]);

  // Load catalog + sessions + system cwd on boot.
  useEffect(() => {
    (async () => {
      const [m, s, sys] = await Promise.all([
        api.listModels(),
        api.listSessions(),
        api.systemCwd().catch(() => null),
      ]);
      setModels(m);
      setSessions(s);
      if (m.length && !pendingModelId) setPendingModelId(m[0].id);
      if (s.length && !activeId) setActiveId(s[0].id);
      if (sys?.cwd) setDefaultCwd(sys.cwd);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist cwd override across reloads.
  useEffect(() => {
    if (cwd) localStorage.setItem(CWD_KEY, cwd);
    else localStorage.removeItem(CWD_KEY);
  }, [cwd]);

  // Persist additional-dirs list. Empty list = remove key entirely.
  useEffect(() => {
    if (additionalDirs.length > 0) {
      localStorage.setItem(ADD_DIRS_KEY, JSON.stringify(additionalDirs));
    } else {
      localStorage.removeItem(ADD_DIRS_KEY);
    }
  }, [additionalDirs]);

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
      cwd: effectiveCwd ?? undefined,
      additional_dirs: additionalDirs.length > 0 ? additionalDirs : null,
      permission_mode: permissionMode,
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
        cwd={cwd}
        defaultCwd={defaultCwd}
        additionalDirs={additionalDirs}
        onChangeProject={(nextCwd, nextDirs) => {
          setCwd(nextCwd);
          setAdditionalDirs(nextDirs);
        }}
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
          permissionMode={permissionMode}
          onPickPermissionMode={setPermissionMode}
          onSelect={setActiveId}
          onCreate={onCreate}
          onDelete={onDelete}
          onClearAll={onClearAll}
        />
        <ErrorBoundary label="ChatPane">
          <ChatPane
            session={active}
            onConfigureFleet={
              active?.provider === "fleet"
                ? () => setFleetEditorOpen(true)
                : undefined
            }
          />
        </ErrorBoundary>
      </div>

      {fleetEditorOpen && (
        <ErrorBoundary label="FleetConfigEditor">
          <FleetConfigEditor
            models={models}
            onCancel={() => setFleetEditorOpen(false)}
            onConfirm={async (override) => {
              setFleetEditorOpen(false);
              await createWithOverride(override);
            }}
          />
        </ErrorBoundary>
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
