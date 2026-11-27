/** evals — 체크 draft 모델 + 직렬화 (CheckRow가 사용하는 순수 로직). */
import type { TFn } from "../../shared/i18n";
import {
  CHECK_TYPES,
  type CheckType, type EvalCheck,
} from "./types";

export interface CheckDraft {
  type: CheckType;
  stage: string;
  value: string;
  pattern: string;
  path: string;
  jsonMode: "equals" | "contains";
  jsonValue: string;
}

export const EMPTY_DRAFT: CheckDraft = {
  type: "output_contains",
  stage: "",
  value: "",
  pattern: "",
  path: "",
  jsonMode: "equals",
  jsonValue: "",
};

export function draftFromCheck(c: EvalCheck): CheckDraft {
  const t = String(c.type || "output_contains") as CheckType;
  return {
    type: CHECK_TYPES.includes(t) ? t : "output_contains",
    stage: String(c.stage || ""),
    value: c.value != null ? String(c.value) : "",
    pattern: String(c.pattern || ""),
    path: String(c.path || ""),
    jsonMode: c.contains !== undefined && c.equals === undefined ? "contains" : "equals",
    jsonValue:
      c.equals !== undefined
        ? JSON.stringify(c.equals)
        : c.contains !== undefined
          ? String(c.contains)
          : "",
  };
}

/** draft → 계약 Check. 실패 시 에러 메시지 문자열 반환. */
export function serializeCheck(d: CheckDraft, t: TFn): EvalCheck | string {
  switch (d.type) {
    case "stage_completed":
      if (!d.stage.trim()) return t("evals.needStage", "스테이지를 입력하세요");
      return { type: d.type, stage: d.stage.trim() };
    case "output_contains":
    case "output_not_contains":
      if (!d.value.trim()) return t("evals.needValue", "값을 입력하세요");
      return { type: d.type, value: d.value };
    case "output_regex":
      if (!d.pattern.trim()) return t("evals.needPattern", "정규식을 입력하세요");
      return { type: d.type, pattern: d.pattern };
    case "max_cost_usd": {
      const n = Number(d.value);
      if (!isFinite(n) || n < 0) return t("evals.badCost", "최대 비용은 0 이상의 숫자여야 합니다");
      return { type: d.type, value: n };
    }
    case "json_path": {
      if (!d.path.trim()) return t("evals.needPath", "JSON 경로를 입력하세요");
      const out: EvalCheck = { type: d.type, path: d.path.trim() };
      if (d.jsonMode === "contains") {
        if (!d.jsonValue.trim()) return t("evals.needValue", "값을 입력하세요");
        out.contains = d.jsonValue;
      } else {
        try {
          out.equals = JSON.parse(d.jsonValue);
        } catch {
          return t("evals.badJsonPath", "JSON 경로 값이 올바르지 않습니다");
        }
      }
      return out;
    }
  }
}
