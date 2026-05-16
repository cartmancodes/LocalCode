import type { SessionRow } from "../types";
import { IconDots, IconLayers, IconMoon, IconPlus, IconSidebar, IconSun } from "./icons";
import ProjectPicker from "./ProjectPicker";

interface Props {
  session: SessionRow | null;
  theme: "light" | "dark";
  wsState?: "connecting" | "open" | "closed";
  cwd: string | null;
  defaultCwd: string | null;
  additionalDirs: string[];
  onChangeProject: (cwd: string | null, additionalDirs: string[]) => void;
  onToggleTheme: () => void;
  onToggleSidebar: () => void;
  onNewChat: () => void;
}

export default function Topbar({
  session,
  theme,
  wsState,
  cwd,
  defaultCwd,
  additionalDirs,
  onChangeProject,
  onToggleTheme,
  onToggleSidebar,
  onNewChat,
}: Props) {
  // Connection dot: idle when no session, pulsing accent while connecting,
  // red on a dropped socket, green when live.
  const conn: "ok" | "err" | "run" | "idle" = !session
    ? "idle"
    : wsState === "connecting"
      ? "run"
      : wsState === "closed"
        ? "err"
        : "ok";
  const connTitle = !session
    ? "no active chat"
    : wsState === "connecting"
      ? "reconnecting…"
      : wsState === "closed"
        ? "disconnected"
        : "connected";

  const fleetLabel = session ? `${session.provider}:${session.model}` : "no fleet";

  return (
    <header className="lc-topbar">
      <div className="lc-titlebar">
        <button className="lc-iconbtn" onClick={onToggleSidebar} aria-label="Toggle sidebar">
          <IconSidebar size={14} />
        </button>
        <span className="lc-tb-title">
          LOCALCODE
          <span className="lc-tb-sep">·</span>
          <span className="lc-tb-sub">{session?.title ?? "Chat"}</span>
        </span>
        <span className="lc-tb-actions">
          <button
            className="lc-iconbtn"
            onClick={onToggleTheme}
            aria-label="Toggle theme"
            title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          >
            {theme === "dark" ? <IconSun size={14} /> : <IconMoon size={14} />}
          </button>
          <button className="lc-iconbtn" aria-label="More">
            <IconDots size={14} />
          </button>
          <button className="lc-primary lc-primary--sm" onClick={onNewChat}>
            <IconPlus size={12} /> New chat
          </button>
        </span>
      </div>

      <div className="lc-subhead">
        <span className="lc-mark">{"{}"}</span>
        <span className="lc-wordmark">LocalCode</span>
        <span className="lc-pill" title={`Active fleet — ${fleetLabel}`}>
          <IconLayers size={11} />
          <span className="lc-pill__txt">{fleetLabel}</span>
        </span>
        <div style={{ flex: 1 }} />
        <ProjectPicker
          cwd={cwd}
          defaultCwd={defaultCwd}
          additionalDirs={additionalDirs}
          onChange={onChangeProject}
        />
        <span
          className={`lc-dot lc-dot--${conn}`}
          title={connTitle}
          aria-label={connTitle}
        />
      </div>
    </header>
  );
}
