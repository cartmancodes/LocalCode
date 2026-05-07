import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import BudgetBar from "./components/BudgetBar";
import ChatPane from "./components/ChatPane";
import Sidebar from "./components/Sidebar";
import type { CatalogModel, SessionRow } from "./types";

export default function App() {
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [pendingModelId, setPendingModelId] = useState<string>("");

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

  const active = useMemo(
    () => sessions.find((s) => s.id === activeId) ?? null,
    [sessions, activeId]
  );

  const onCreate = async () => {
    if (!pendingModelId) return;
    const [provider, model] = pendingModelId.split(":") as [SessionRow["provider"], string];
    const fresh = await api.createSession({ provider, model });
    setSessions((cur) => [fresh, ...cur]);
    setActiveId(fresh.id);
  };

  const onDelete = async (id: string) => {
    await api.deleteSession(id);
    setSessions((cur) => cur.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  };

  return (
    <div className="layout">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        models={models}
        pendingModelId={pendingModelId}
        onPickModel={setPendingModelId}
        onSelect={setActiveId}
        onCreate={onCreate}
        onDelete={onDelete}
      />
      <ChatPane session={active} />
      <div className="footer">
        <BudgetBar />
      </div>
    </div>
  );
}
