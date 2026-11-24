// Mirrors backend Pydantic models (app/orchestrator.py, app/api/routes.py). Keep in sync via PR review;
// the contract test in tests/test_api.py pins the server side.

export type Outcome = "pass" | "redacted" | "amended" | "routed" | "blocked";
export type Persona = "rep" | "manager" | "compliance";

export interface Verdict {
  rule_id: string;
  stage: "input" | "output" | "authz";
  outcome: Outcome;
  detail: string;
}

export interface ToolCall {
  tool: string;
  dependency: string;
  identity: string;
  scope: string | null;
  token_fingerprint: string | null;
  duration_ms: number;
  status: "ok" | "error" | "circuit_open";
  detail: string;
}

export interface TraceStep {
  step: string;
  status: string;
  detail: string;
  ms: number;
}

export interface RouteDecision {
  intent: string;
  confidence: number;
  method: string;
  rationale: string;
}

export interface PendingActionView {
  action_id: string;
  kind: string;
  summary: string;
  expires_at: string;
}

export interface ChartData {
  metric: "trx" | "nrx" | "market_share";
  product: string;
  weeks: string[];
  series: Record<string, number[]>;
  total: number[];
}

export interface TurnResult {
  turn_id: string;
  correlation_id: string;
  conversation_id: string;
  answer: string;
  intent: string;
  agent: string;
  route: RouteDecision;
  verdicts: Verdict[];
  tool_calls: ToolCall[];
  citations: string[];
  pending_action: PendingActionView | null;
  data: (Record<string, unknown> & { chart?: ChartData; slots?: string[] }) | null;
  degraded: boolean;
  blocked: boolean;
  trace: TraceStep[];
  latency_ms: number;
  policy_version: string;
  kb_version: string;
  audit_seq: number;
  audit_hash: string;
}

export interface ActionResult {
  action_id: string;
  kind: string;
  status: "completed" | "failed" | "cancelled";
  message: string;
  tool_calls: ToolCall[];
  record: Record<string, unknown> | null;
  audit_seq: number;
}

export interface Me {
  oid: string;
  name: string;
  email: string;
  roles: string[];
  territory: string;
}

export interface AuditEvent {
  seq: number;
  event_id: string;
  ts: string;
  correlation_id: string;
  actor_oid: string;
  actor_roles: string[];
  event_type: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface ChainVerification {
  valid: boolean;
  records_checked: number;
  first_broken_seq: number | null;
  head_hash: string;
}

export interface Breaker {
  name: string;
  state: "closed" | "open" | "half_open";
  consecutive_failures: number;
}

export interface OpsStatus {
  environment: string;
  version: string;
  auth_mode: string;
  llm: string;
  kb_version: string;
  kb_documents: { doc_id: string; title: string; mlr_id: string | null; indexed: boolean; exclusion_reason: string | null }[];
  integrations: Record<string, "live" | "mock">;
  breakers: Breaker[];
  faults: Record<string, string>;
  fault_injection_allowed: boolean;
  answer_cache: { hits: number; misses: number };
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown>; correlation_id: string };
}
