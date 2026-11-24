import { useEffect, useState } from "react";
import { AuditPanel } from "./components/AuditPanel";
import { ChatPanel, type ChatMessage } from "./components/ChatPanel";
import { OpsPanel } from "./components/OpsPanel";
import { TracePanel } from "./components/TracePanel";
import { api } from "./lib/api";
import { authMode } from "./lib/auth";
import type { Me, Persona, TurnResult } from "./lib/types";

type Tab = "trace" | "ops" | "audit";

const PERSONA_LABEL: Record<Persona, string> = {
  rep: "Field Rep",
  manager: "District Manager",
  compliance: "Compliance",
};

export default function App() {
  const [persona, setPersona] = useState<Persona>("rep");
  const [me, setMe] = useState<Me | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [selected, setSelected] = useState<TurnResult | null>(null);
  const [tab, setTab] = useState<Tab>("trace");
  const [refreshKey, setRefreshKey] = useState(0);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    setMessages([]);
    setConversationId(undefined);
    setSelected(null);
    if (persona !== "compliance" && tab === "audit") setTab("trace");
    api
      .me(persona)
      .then((m) => {
        setMe(m);
        setAuthError(null);
      })
      .catch((e) => setAuthError(String(e.message ?? e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona]);

  const onTurn = (t: TurnResult) => {
    setSelected(t);
    setRefreshKey((k) => k + 1);
  };

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="logo">℞</span>
          <div>
            <b>PharmaFieldAssist</b>
            <small>Contoso Pharma · Field Sales AI</small>
          </div>
        </div>
        <div className="who">
          {authMode === "demo" && (
            <div className="personas" role="tablist" aria-label="Demo persona">
              {(Object.keys(PERSONA_LABEL) as Persona[]).map((p) => (
                <button key={p} className={p === persona ? "active" : ""} onClick={() => setPersona(p)}>
                  {PERSONA_LABEL[p]}
                </button>
              ))}
            </div>
          )}
          {me && (
            <div className="me">
              <b>{me.name}</b>
              <small>
                {me.roles.join(" · ")} · {me.territory}
              </small>
            </div>
          )}
          <span className="chip info">{authMode === "demo" ? "demo auth" : "Entra ID"}</span>
        </div>
      </header>
      {authError && <div className="banner error">Sign-in failed: {authError}</div>}

      <main>
        <ChatPanel
          persona={persona}
          messages={messages}
          setMessages={setMessages}
          conversationId={conversationId}
          setConversationId={setConversationId}
          onTurn={onTurn}
          onSelect={setSelected}
          selectedTurnId={selected?.turn_id ?? null}
        />
        <aside>
          <nav className="tabs">
            <button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}>
              Agent trace
            </button>
            <button className={tab === "ops" ? "active" : ""} onClick={() => setTab("ops")}>
              Platform ops
            </button>
            {persona === "compliance" && (
              <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>
                Audit log
              </button>
            )}
          </nav>
          <div className="panel">
            {tab === "trace" && <TracePanel turn={selected} />}
            {tab === "ops" && <OpsPanel persona={persona} refreshKey={refreshKey} />}
            {tab === "audit" && <AuditPanel persona={persona} refreshKey={refreshKey} />}
          </div>
        </aside>
      </main>
    </div>
  );
}
