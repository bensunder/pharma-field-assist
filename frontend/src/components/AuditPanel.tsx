import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { AuditEvent, ChainVerification, Persona } from "../lib/types";

const TYPES = ["", "chat.turn", "chat.blocked", "ae.flagged", "action.completed", "action.failed", "ops.fault_injected"];

export function AuditPanel({ persona, refreshKey }: { persona: Persona; refreshKey: number }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [verify, setVerify] = useState<ChainVerification | null>(null);
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ev, v] = await Promise.all([api.audit(persona, filter || undefined), api.verify(persona)]);
      setEvents(ev);
      setVerify(v);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.message}` : String(e));
    }
  }, [persona, filter]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  if (error) return <p className="error pad">{error}</p>;

  return (
    <div className="pad audit">
      <div className="row">
        {verify && (
          <span className={`chip ${verify.valid ? "ok" : "bad"}`}>
            Hash chain {verify.valid ? "verified" : `BROKEN at #${verify.first_broken_seq}`} · {verify.records_checked} records
          </span>
        )}
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter event type">
          {TYPES.map((t) => (
            <option key={t} value={t}>
              {t || "all events"}
            </option>
          ))}
        </select>
        <button onClick={() => void load()}>Refresh</button>
      </div>
      <table className="tools">
        <thead>
          <tr>
            <th>#</th>
            <th>Time (UTC)</th>
            <th>Event</th>
            <th>Actor</th>
            <th>Summary</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <Fragment key={e.seq}>
              <tr onClick={() => setOpen(open === e.seq ? null : e.seq)} className="clickable">
                <td>{e.seq}</td>
                <td>{e.ts.slice(11, 19)}</td>
                <td>
                  <span className={`chip ${e.event_type.includes("blocked") || e.event_type.startsWith("ae") ? "bad" : "info"}`}>
                    {e.event_type}
                  </span>
                </td>
                <td>
                  <small>{e.actor_roles.join(", ")}</small>
                </td>
                <td>
                  <small>{summarize(e)}</small>
                </td>
              </tr>
              {open === e.seq && (
                <tr>
                  <td colSpan={5}>
                    <pre>{JSON.stringify({ ...e.payload, prev_hash: e.prev_hash, hash: e.hash }, null, 2)}</pre>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function summarize(e: AuditEvent): string {
  const p = e.payload as Record<string, unknown>;
  if (e.event_type.startsWith("chat")) return `${p.agent} · ${String(p.input_sanitized ?? "").slice(0, 60)}`;
  if (e.event_type === "ae.flagged") return `${p.ae_case_ref} · due ${String(p.due_utc).slice(0, 16)}`;
  if (e.event_type.startsWith("action")) return `${p.kind}`;
  if (e.event_type.startsWith("ops")) return JSON.stringify(p);
  return "";
}
