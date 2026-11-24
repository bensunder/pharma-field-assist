import type { TurnResult, Verdict } from "../lib/types";

const OUTCOME_CLASS: Record<Verdict["outcome"], string> = {
  pass: "ok",
  redacted: "warn",
  amended: "warn",
  routed: "info",
  blocked: "bad",
};

export function TracePanel({ turn }: { turn: TurnResult | null }) {
  if (!turn) return <p className="muted pad">Send a message to see the agent trace: routing, tool calls, compliance verdicts and audit.</p>;
  const flagged = turn.verdicts.filter((v) => v.outcome !== "pass");
  return (
    <div className="trace pad">
      <section>
        <h4>Route</h4>
        <div className="kv">
          <span>Intent</span>
          <b>{turn.intent}</b>
          <span>Agent</span>
          <b>{turn.agent}</b>
          <span>Method</span>
          <code>{turn.route.method}</code>
          <span>Confidence</span>
          <b>{turn.route.confidence.toFixed(2)}</b>
          {turn.route.rationale && (
            <>
              <span>Signals</span>
              <small>{turn.route.rationale}</small>
            </>
          )}
        </div>
      </section>

      <section>
        <h4>Pipeline · {turn.latency_ms.toFixed(1)} ms</h4>
        <ol className="steps">
          {turn.trace.map((s, i) => (
            <li key={i} className={`step ${s.status}`}>
              <span className="dot" />
              <span className="name">{s.step}</span>
              <span className="status">{s.status}</span>
              <span className="ms">{s.ms.toFixed(1)}ms</span>
              {s.detail && <small className="detail">{s.detail}</small>}
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h4>Compliance verdicts</h4>
        <div className="chips">
          {turn.verdicts.map((v, i) => (
            <span key={i} className={`chip ${OUTCOME_CLASS[v.outcome]}`} title={v.detail}>
              {v.rule_id} · {v.outcome}
            </span>
          ))}
        </div>
        {flagged.map((v, i) => (
          <p key={i} className="small">
            <b>{v.rule_id}</b> {v.detail}
          </p>
        ))}
      </section>

      {turn.tool_calls.length > 0 && (
        <section>
          <h4>Tool calls</h4>
          <table className="tools">
            <thead>
              <tr>
                <th>Tool</th>
                <th>Identity</th>
                <th>Status</th>
                <th>ms</th>
              </tr>
            </thead>
            <tbody>
              {turn.tool_calls.map((t, i) => (
                <tr key={i} className={t.status}>
                  <td>
                    <code>{t.tool}</code>
                    {t.scope && <small className="block">scope: {t.scope}</small>}
                  </td>
                  <td>
                    <small>{t.identity}</small>
                    {t.token_fingerprint && <small className="block">token #{t.token_fingerprint}</small>}
                  </td>
                  <td>
                    <span className={`chip ${t.status === "ok" ? "ok" : "bad"}`}>{t.status}</span>
                  </td>
                  <td>{t.duration_ms.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {turn.citations.length > 0 && (
        <section>
          <h4>Approved sources</h4>
          <ol className="small">
            {turn.citations.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ol>
        </section>
      )}

      <section>
        <h4>Audit</h4>
        <div className="kv small">
          <span>Record</span>
          <code>#{turn.audit_seq}</code>
          <span>Hash</span>
          <code>{turn.audit_hash.slice(0, 20)}…</code>
          <span>Correlation</span>
          <code>{turn.correlation_id}</code>
          <span>Policy / KB</span>
          <code>
            {turn.policy_version} / {turn.kb_version}
          </code>
        </div>
      </section>
    </div>
  );
}
