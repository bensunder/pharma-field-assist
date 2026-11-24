import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { OpsStatus, Persona } from "../lib/types";

const DEPS = ["graph", "crm", "fabric"] as const;
const LABEL: Record<string, string> = { graph: "Microsoft Graph (Outlook)", crm: "Veeva CRM", fabric: "Microsoft Fabric" };

export function OpsPanel({ persona, refreshKey }: { persona: Persona; refreshKey: number }) {
  const [status, setStatus] = useState<OpsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canOperate = persona !== "rep";

  const load = useCallback(async () => {
    try {
      setStatus(await api.ops(persona));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message} (${e.correlationId})` : String(e));
    }
  }, [persona]);

  useEffect(() => {
    void load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load, refreshKey]);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  if (!status) return <p className="muted pad">{error ?? "Loading…"}</p>;

  return (
    <div className="pad ops">
      <div className="kv small">
        <span>Environment</span>
        <b>{status.environment}</b>
        <span>Version</span>
        <b>{status.version}</b>
        <span>Auth</span>
        <b>{status.auth_mode}</b>
        <span>LLM</span>
        <b>{status.llm}</b>
        <span>Answer cache</span>
        <b>
          {status.answer_cache.hits} hits / {status.answer_cache.misses} misses
        </b>
      </div>

      <h4>Dependencies</h4>
      {DEPS.map((d) => {
        const b = status.breakers.find((x) => x.name === d);
        const fault = status.faults[d] ?? "none";
        return (
          <div key={d} className={`dep ${b?.state}`}>
            <div>
              <b>{LABEL[d]}</b>{" "}
              <span className={`chip ${status.integrations[d] === "live" ? "ok" : "info"}`}>{status.integrations[d]}</span>
              <small className="block">
                breaker <span className={`chip ${b?.state === "closed" ? "ok" : "bad"}`}>{b?.state}</span> · failures{" "}
                {b?.consecutive_failures} · fault: {fault}
              </small>
            </div>
            {canOperate && status.fault_injection_allowed && (
              <div className="btns">
                <select
                  value={fault}
                  onChange={(e) => act(() => api.setFault(persona, d, e.target.value as "none" | "error" | "timeout"))}
                  aria-label={`Inject fault into ${d}`}
                >
                  <option value="none">healthy</option>
                  <option value="error">inject 503</option>
                  <option value="timeout">inject timeout</option>
                </select>
                <button onClick={() => act(() => api.resetBreaker(persona, d))} disabled={b?.state === "closed"}>
                  Reset
                </button>
              </div>
            )}
          </div>
        );
      })}
      {!canOperate && <p className="small muted">Incident controls require DistrictManager or ComplianceOfficer (switch persona).</p>}

      <h4>Knowledge base · {status.kb_version}</h4>
      <ul className="docs small">
        {status.kb_documents.map((d) => (
          <li key={d.doc_id} className={d.indexed ? "" : "excluded"}>
            <span className={`chip ${d.indexed ? "ok" : "bad"}`}>{d.indexed ? "indexed" : "excluded"}</span> {d.title}{" "}
            <code>{d.mlr_id}</code>
            {d.exclusion_reason && <small className="block">{d.exclusion_reason}</small>}
          </li>
        ))}
      </ul>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
