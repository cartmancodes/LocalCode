import { useEffect, useState } from "react";
import { api } from "../api";
import type { Budget } from "../types";

export default function BudgetBar() {
  const [b, setB] = useState<Budget | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await api.budget();
        if (!cancelled) setB(next);
      } catch {
        /* ignore — proxy may be offline */
      }
    };
    tick();
    const id = setInterval(tick, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (!b) return <div className="budget-bar muted">budget …</div>;
  const pct = Math.min(100, Math.round((b.spend_usd / Math.max(b.daily_budget_usd, 0.01)) * 100));
  const danger = pct >= 80;
  return (
    <div className={`budget-bar ${danger ? "danger" : ""}`}>
      <div className="budget-fill" style={{ width: `${pct}%` }} />
      <span className="budget-label">
        ${b.spend_usd.toFixed(2)} / ${b.daily_budget_usd.toFixed(2)} today
      </span>
    </div>
  );
}
