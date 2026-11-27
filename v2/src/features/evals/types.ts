/** evals — API 계약 타입(모든 필드 옵셔널 — 백엔드 응답 편차 방어) + 순수 헬퍼.
 *  API 계약(백엔드 병렬 구현):
 *    GET    /evals?status=            → { items: EvalItem[] }
 *    POST   /evals                    → EvalItem
 *    GET    /evals/{id}               → EvalItem
 *    PATCH  /evals/{id}               → EvalItem (partial)
 *    DELETE /evals/{id}               → 보관 처리
 *    POST   /evals/{id}/run           → { eval_run_id, run_id, status }
 *    GET    /evals/{id}/runs          → { items: EvalRun[] } (최신순)
 */
import { fmtAge } from "../../shared/lib/format";
import type { TFn } from "../../shared/i18n";

export type CheckType =
  | "stage_completed"
  | "output_contains"
  | "output_not_contains"
  | "output_regex"
  | "json_path"
  | "max_cost_usd";

export interface EvalCheck {
  type?: string;
  stage?: string;
  value?: unknown;
  pattern?: string;
  path?: string;
  equals?: unknown;
  contains?: unknown;
}

export interface EvalItem {
  id: string;
  name?: string;
  workflow_key?: string;
  input?: Record<string, unknown>;
  checks?: EvalCheck[];
  status?: string; // "active" | "archived"
  created_at?: string;
  updated_at?: string;
  last_score?: number | null;
  last_run_at?: string | null;
}

export interface EvalRunResult {
  // 채점 결과 1건 — check_type은 표시용 타입 문자열, check는 원본 체크
  // 객체(파라미터 보존, 툴팁에 JSON.stringify로 표시). 레거시 응답 대비
  // check가 문자열/객체일 수 있어 옵셔널로 둔다.
  check_type?: string;
  check?: EvalCheck | Record<string, unknown> | string | null;
  passed?: boolean;
  detail?: string;
}

export interface EvalRun {
  id: string;
  run_id?: string | null;
  status?: string; // "pending" | "scored" | "error"
  score?: number | null;
  results?: EvalRunResult[];
  error?: string | null;
  created_at?: string;
  scored_at?: string | null;
}

export interface EvalsResponse { items?: EvalItem[] }
export interface EvalRunsResponse { items?: EvalRun[] }
export interface EvalRunResponse { eval_run_id?: string; run_id?: string; status?: string }

export const CHECK_TYPES: CheckType[] = [
  "stage_completed",
  "output_contains",
  "output_not_contains",
  "output_regex",
  "json_path",
  "max_cost_usd",
];

export const inputCls =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-accent";

export function statusOf(e: EvalItem): string {
  return String(e.status || "active").toLowerCase();
}

export function scoreTone(score: number | null | undefined): "ok" | "warn" | "err" | "muted" {
  if (score == null || !isFinite(Number(score))) return "muted";
  if (Number(score) >= 80) return "ok";
  if (Number(score) >= 50) return "warn";
  return "err";
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "-";
  return fmtAge(ms / 1000);
}

export function checkLabel(t: TFn, type?: string): string {
  switch (type) {
    case "stage_completed": return t("evals.chk.stage_completed", "스테이지 완료");
    case "output_contains": return t("evals.chk.output_contains", "출력 포함");
    case "output_not_contains": return t("evals.chk.output_not_contains", "출력 미포함");
    case "output_regex": return t("evals.chk.output_regex", "출력 정규식");
    case "json_path": return t("evals.chk.json_path", "JSON 경로");
    case "max_cost_usd": return t("evals.chk.max_cost_usd", "최대 비용");
    default: return type || "?";
  }
}
