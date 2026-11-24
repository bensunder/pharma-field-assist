import { getToken } from "./auth";
import type {
  ActionResult,
  ApiErrorBody,
  AuditEvent,
  ChainVerification,
  Me,
  OpsStatus,
  Persona,
  TurnResult,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public correlationId: string | null,
  ) {
    super(message);
  }
}

export function newCorrelationId(): string {
  return crypto.randomUUID();
}

async function request<T>(persona: Persona, path: string, init: RequestInit = {}): Promise<T> {
  const token = await getToken(persona);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const correlationId = newCorrelationId();
  try {
    const r = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        // Not Authorization: that header carries the edge basic-auth credentials on demo hosts.
        "X-PFA-Token": token,
        "X-Correlation-ID": correlationId,
        ...(init.headers ?? {}),
      },
    });
    if (!r.ok) {
      let body: ApiErrorBody | null = null;
      try {
        body = (await r.json()) as ApiErrorBody;
      } catch {
        /* non-JSON error (e.g. ingress 502) */
      }
      throw new ApiError(
        r.status,
        body?.error.code ?? "http_error",
        body?.error.message ?? `Request failed (${r.status})`,
        body?.error.correlation_id ?? r.headers.get("X-Correlation-ID") ?? correlationId,
      );
    }
    return (await r.json()) as T;
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError(0, "timeout", "The request timed out", correlationId);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  me: (p: Persona) => request<Me>(p, "/api/me"),
  chat: (p: Persona, message: string, conversationId?: string) =>
    request<TurnResult>(p, "/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: conversationId }),
    }),
  confirm: (p: Persona, id: string) => request<ActionResult>(p, `/api/actions/${id}/confirm`, { method: "POST" }),
  cancel: (p: Persona, id: string) => request<ActionResult>(p, `/api/actions/${id}/cancel`, { method: "POST" }),
  audit: (p: Persona, eventType?: string) =>
    request<AuditEvent[]>(p, `/api/audit?limit=100${eventType ? `&event_type=${encodeURIComponent(eventType)}` : ""}`),
  verify: (p: Persona) => request<ChainVerification>(p, "/api/audit/verify"),
  ops: (p: Persona) => request<OpsStatus>(p, "/api/ops/status"),
  setFault: (p: Persona, dependency: string, mode: "none" | "error" | "timeout") =>
    request<{ faults: Record<string, string> }>(p, "/api/ops/faults", {
      method: "POST",
      body: JSON.stringify({ dependency, mode }),
    }),
  resetBreaker: (p: Persona, name: string) => request<unknown>(p, `/api/ops/breakers/${name}/reset`, { method: "POST" }),
};
