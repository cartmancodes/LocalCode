import { useEffect, useState } from "react";
import { api } from "../api";
import type { Budget } from "../types";

/**
 * Replaces the old single-bar BudgetBar. The "Session" row was originally a
 * stacked per-agent bar in the design — we don't yet track per-agent cost in
 * the backend (cost only arrives on `assistant.done`), so for now we render
 * a single accent fill with the latest assistant.done cost. The "Today" row
 * still polls /api/budget for daily spend vs cap.
 */
interface Props {
  sessionCostUsd?: number;
}

export default function CostMeter({ sessionCostUsd = 0 }: Props) {
  const [b, setB] = useState<Budget | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await api.budget();
        if (!cancelled) setB(next);
      } catch {
        /* ignore */
      }
    };
    tick();
    const id = setInterval(tick, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const dailyCap = b?.daily_budget_usd ?? 10;
  const dailySpent = b?.spend_usd ?? 0;
  // Session bar caps at $0.50 by default so a typical turn shows visible fill.
  const sessionCap = Math.max(0.5, sessionCostUsd * 1.2);

  return (
    <div className="lc-cost">
      <div className="lc-cost__row">
        <div className="lc-cost__lbl">
          <span className="lc-cost__title">Session</span>
          <span className="lc-cost__val">${sessionCostUsd.toFixed(3)}</span>
        </div>
        <div className="lc-cost__bar">
          <span
            className="lc-cost__fill"
            style={{ width: `${Math.min(100, (sessionCostUsd / sessionCap) * 100)}%` }}
          />
        </div>
        <div className="lc-cost__legend">
          <span className="lc-cost__leg">
            <span className="lc-cost__dot" style={{ background: "var(--accent)" }} />
            this turn
          </span>
        </div>
      </div>
      <div className="lc-cost__row lc-cost__row--day">
        <div className="lc-cost__lbl">
          <span className="lc-cost__title">Today</span>
          <span className="lc-cost__val">
            ${dailySpent.toFixed(2)}{" "}
            <span className="lc-cost__lim">/ ${dailyCap.toFixed(2)}</span>
          </span>
        </div>
        <div className="lc-cost__bar">
          <span
            className="lc-cost__fill"
            style={{ width: `${Math.min(100, (dailySpent / Math.max(dailyCap, 0.01)) * 100)}%` }}
          />
        </div>
      </div>
    </div>
  );
}
