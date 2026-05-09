import type { SessionRow } from "../types";
import {
  IconDots,
  IconLogo,
  IconMoon,
  IconPlus,
  IconSidebar,
  IconSun,
} from "./icons";
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
  // Map WS state → user-facing pip status. With no active session we show idle.
  const status: "running" | "done" | "error" | "idle" =
    !session ? "idle"
    : wsState === "connecting" ? "running"
    : wsState === "closed" ? "error"
    : "done";
  const statusLabel = !session
    ? "idle"
    : wsState === "connecting" ? "reconnecting"
    : wsState === "closed" ? "disconnected"
    : "connected";

  return (
    <header className="lc-topbar">
      <div className="lc-topbar__left">
        <button className="lc-iconbtn" onClick={onToggleSidebar} aria-label="Toggle sidebar">
          <IconSidebar />
        </button>
        <div className="lc-brand">
          <span className="lc-brand__mark"><IconLogo /></span>
          <span className="lc-brand__name">LocalCode</span>
        </div>
        <span className="lc-divider" />
        <nav className="lc-breadcrumb">
          <span className="lc-bc__fleet">{session?.provider ?? "—"}</span>
          <span className="lc-bc__sep">/</span>
          <span className="lc-bc__chat">{session?.title ?? "New chat"}</span>
          <span className={`lc-pip lc-pip--${status}`}>
            <span className="lc-pip__dot" />
            {statusLabel}
          </span>
        </nav>
      </div>
      <div className="lc-topbar__right">
        <ProjectPicker
          cwd={cwd}
          defaultCwd={defaultCwd}
          additionalDirs={additionalDirs}
          onChange={onChangeProject}
        />
        <button
          className="lc-iconbtn"
          onClick={onToggleTheme}
          aria-label="Toggle theme"
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
        >
          {theme === "dark" ? <IconSun /> : <IconMoon />}
        </button>
        <button className="lc-iconbtn" aria-label="More"><IconDots /></button>
        <button className="lc-primary lc-primary--sm" onClick={onNewChat}>
          <IconPlus size={14} /> New chat
        </button>
      </div>
    </header>
  );
}
