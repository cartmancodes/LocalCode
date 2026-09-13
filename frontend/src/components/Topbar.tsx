import { useEffect, useState } from "react";
import { api } from "../api";
import type { QuotaProvider, QuotaSnapshot, SessionRow } from "../types";
import { IconDots, IconLayers, IconMoon, IconPlus, IconSidebar, IconSun } from "./icons";
import ProjectPicker from "./ProjectPicker";

interface Props {
  session: SessionRow | null;
  /** Bumped by ChatPane when a turn ends — the only moment the quota numbers
   * can have moved, and so the only thing that refetches them. */
  turnEpoch?: number;
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
  turnEpoch,
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
  const quota = useQuota(session?.id, turnEpoch);

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
        {quota ? <QuotaMeters snapshot={quota} /> : null}
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

/** The quota snapshot, refetched on session change and turn completion.
 *
 * No interval: the number only moves when a turn finishes, and a poll that
 * fires every N seconds would spend requests proving that nothing happened.
 * A failed fetch leaves the last good snapshot up rather than blanking the
 * meter — a missing bar reads as "you have no quota", which is a worse lie
 * than a slightly stale one.
 */
function useQuota(sessionId: string | undefined, turnEpoch: number | undefined) {
  const [snapshot, setSnapshot] = useState<QuotaSnapshot | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .getQuota()
      .then((next) => {
        if (alive) setSnapshot(next);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [sessionId, turnEpoch]);
  return snapshot;
}

/** One compact bar per subscription: how much of the plan is LEFT.
 *
 * The prominent always-visible number is headroom, not dollars — the
 * per-message `cost_usd` stays in the transcript where it was. An unmeasured
 * provider draws a hatched "unknown" bar instead of a full one: a plan nobody
 * has measured is not a plan that is known to be empty, and it is certainly
 * not one that is known to be full.
 */
function QuotaMeters({ snapshot }: { snapshot: QuotaSnapshot }) {
  const names = Object.keys(snapshot.providers).sort();
  if (names.length === 0) return null;
  return (
    <span className="lc-quota" role="group" aria-label="Subscription headroom">
      {names.map((name) => (
        <QuotaMeter
          key={name}
          name={name}
          info={snapshot.providers[name]}
          threshold={snapshot.queue_threshold}
        />
      ))}
    </span>
  );
}

function QuotaMeter({
  name,
  info,
  threshold,
}: {
  name: string;
  info: QuotaProvider;
  threshold: number;
}) {
  const unknown = info.confidence === "unknown";
  const pct = Math.round(Math.max(0, Math.min(1, info.headroom)) * 100);
  const level = unknown ? "unknown" : info.headroom < threshold ? "err" : info.headroom < 0.25 ? "warn" : "ok";
  const resets = info.resets_at ? new Date(info.resets_at * 1000).toLocaleString() : null;
  const title = unknown
    ? `${name}: no limit reported yet — usage is being tracked locally, so this is an estimate, not a measurement`
    : [
        `${name}: ${pct}% of the plan left`,
        resets ? `resets ${resets}` : null,
        ...info.windows.map(
          (w) => `${w.key}: ${Math.round(w.headroom * 100)}% left (${w.source})`,
        ),
      ]
        .filter(Boolean)
        .join(" · ");
  return (
    <span className={`lc-quota__item lc-quota__item--${level}`} title={title}>
      <span className="lc-quota__name">{name}</span>
      <span className="lc-quota__bar" aria-hidden="true">
        <span className="lc-quota__fill" style={{ width: `${unknown ? 100 : pct}%` }} />
      </span>
      <span className="lc-quota__pct">{unknown ? "?" : `${pct}%`}</span>
    </span>
  );
}
