/** AgentPermissionsSection — GET/PUT /management/agents/{id}/permissions (spec §3차 D/F).
 *
 *  규칙 1건 = {capability, effect: allow|deny, scope_type: global|project|task, scope_id?}.
 *  명세는 GET/PUT만 정의 — PUT은 규칙 집합 전체를 교체하는 것으로 가정하고
 *  추가/삭제 시 현재 목록을 수정해 통째로 PUT한다(개별 POST/DELETE 없음).
 *
 *  기본값 안내(spec D): external:send / deploy:execute / destructive:execute는
 *  기본 deny이며 명시적 allow + 2차 approval이 모두 필요하다. 버튼 숨김은
 *  편의일 뿐 서버가 다시 검증한다. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, api, ApiError } from "../../shared/api/client";
import type { AgentPermission, AgentPermissionsResponse } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { Badge, Button, Spinner, ErrorBox } from "../../shared/ui";

/** spec §3차 D capability 최소 집합. */
const CAPABILITIES = [
  "task:read", "task:create", "task:assign", "task:update",
  "agent:read", "agent:manage", "agent:wake",
  "run:execute", "run:approve",
  "cost:read", "budget:manage",
  "artifact:create",
  "external:send", "deploy:execute", "destructive:execute",
  "integration:manage",
];
/** 기본 deny + 2차 approval이 필요한 위험 capability (spec D). */
const RISKY = new Set(["external:send", "deploy:execute", "destructive:execute"]);
const SCOPE_TYPES = ["global", "project", "task"] as const;

const EFFECT_TONE: Record<string, "ok" | "err" | "muted"> = {
  allow: "ok",
  deny: "err",
};

const inputCls =
  "rounded-md border border-border bg-surface px-2 py-1 text-xs outline-none focus:border-accent";

function normRules(data: AgentPermissionsResponse | AgentPermission[] | null | undefined): AgentPermission[] {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.items)) return data.items;
  if (data && Array.isArray(data.permissions)) return data.permissions;
  return [];
}

/** 규칙 dedupe 키 — UNIQUE(agent_id, capability, scope_type, scope_id, effect)와 동일. */
function ruleKey(r: AgentPermission): string {
  return [r.capability || "", r.effect || "", r.scope_type || "", r.scope_id || ""].join("|");
}

export function AgentPermissionsSection({ agentId }: { agentId: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [capability, setCapability] = useState(CAPABILITIES[0]);
  const [effect, setEffect] = useState<"allow" | "deny">("allow");
  const [scopeType, setScopeType] = useState<(typeof SCOPE_TYPES)[number]>("global");
  const [scopeId, setScopeId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const path = `/management/agents/${encodeURIComponent(agentId)}/permissions`;
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["agent-permissions", agentId],
    queryFn: () => apiGet<AgentPermissionsResponse | AgentPermission[]>(path),
  });
  const rules = useMemo(() => normRules(data), [data]);

  /** PUT 전체 교체 — 성공 시 목록 재조회. */
  const putRules = async (next: AgentPermission[]) => {
    setBusy(true);
    setErr("");
    try {
      await api(path, {
        method: "PUT",
        json: {
          permissions: next.map((r) => ({
            capability: r.capability,
            effect: r.effect,
            scope_type: r.scope_type,
            // 스키마는 scope_id NOT NULL DEFAULT '' — global은 빈 문자열.
            scope_id: r.scope_id || "",
          })),
        },
      });
      await qc.invalidateQueries({ queryKey: ["agent-permissions", agentId] });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const add = () => {
    if (scopeType !== "global" && !scopeId.trim()) {
      setErr(t("perm.needScopeId", "project/task 범위는 scope ID를 입력하세요"));
      return;
    }
    const rule: AgentPermission = {
      capability,
      effect,
      scope_type: scopeType,
      scope_id: scopeType === "global" ? "" : scopeId.trim(),
    };
    if (rules.some((r) => ruleKey(r) === ruleKey(rule))) {
      setErr(t("perm.duplicate", "동일한 규칙이 이미 있습니다"));
      return;
    }
    void putRules([...rules, rule]);
    setScopeId("");
  };

  const remove = (idx: number) => {
    void putRules(rules.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <div className="text-xs font-bold text-muted">{t("perm.title", "권한 규칙")}</div>
        <button type="button" className="text-[11px] text-muted hover:text-ink" onClick={() => refetch()}>
          {t("common.refresh", "새로고침")}
        </button>
      </div>
      <p className="mb-2 text-[11px] leading-relaxed text-muted">
        {t("perm.note", "external:send / deploy:execute / destructive:execute는 기본 deny — 명시적 allow와 2차 승인이 모두 필요합니다. 규칙 평가는 deny 우선, 범위는 task > project > global 순입니다.")}
      </p>
      {isLoading && <Spinner />}
      {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
      {data && !rules.length && (
        <div className="mb-2 text-xs text-muted">{t("perm.empty", "등록된 권한 규칙이 없습니다 — 기본 정책만 적용됩니다")}</div>
      )}
      <ul className="mb-2 space-y-1">
        {rules.map((r, i) => {
          const cap = String(r.capability || "");
          const eff = String(r.effect || "");
          const scope = String(r.scope_type || "global");
          return (
            <li
              key={r.id || `${ruleKey(r)}-${i}`}
              className="flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface-hover px-2 py-1.5"
            >
              <Badge tone={EFFECT_TONE[eff] ?? "muted"}>{eff === "deny" ? t("perm.deny", "deny") : t("perm.allow", "allow")}</Badge>
              <span className="font-mono text-xs font-medium text-ink">{cap}</span>
              {RISKY.has(cap) && <Badge tone="warn">{t("perm.risky", "위험")}</Badge>}
              <span className="text-[11px] text-muted">
                {scope}{r.scope_id ? `:${r.scope_id}` : ""}
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => remove(i)}
                aria-label={t("common.delete", "삭제")}
                className="ml-auto rounded px-1.5 py-0.5 text-[11px] text-err hover:bg-err-soft"
              >
                ✕
              </button>
            </li>
          );
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-1.5">
        <select value={capability} onChange={(e) => setCapability(e.target.value)} className={inputCls} aria-label={t("perm.capability", "capability")}>
          {CAPABILITIES.map((c) => (
            <option key={c} value={c}>{c}{RISKY.has(c) ? " ⚠" : ""}</option>
          ))}
        </select>
        <select value={effect} onChange={(e) => setEffect(e.target.value as "allow" | "deny")} className={inputCls} aria-label={t("perm.effect", "효과")}>
          <option value="allow">{t("perm.allow", "allow")}</option>
          <option value="deny">{t("perm.deny", "deny")}</option>
        </select>
        <select value={scopeType} onChange={(e) => setScopeType(e.target.value as typeof scopeType)} className={inputCls} aria-label={t("perm.scopeType", "범위 유형")}>
          {SCOPE_TYPES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        {scopeType !== "global" && (
          <input
            value={scopeId}
            onChange={(e) => setScopeId(e.target.value)}
            placeholder={scopeType === "project" ? t("perm.scopeProjectPh", "프로젝트 ID") : t("perm.scopeTaskPh", "태스크 ID")}
            className={`${inputCls} w-32`}
          />
        )}
        <Button variant="secondary" className="px-2 py-1 text-xs" disabled={busy} onClick={add}>
          {t("perm.add", "규칙 추가")}
        </Button>
      </div>
      {err && <p className="mt-1 text-xs text-err">{err}</p>}
    </div>
  );
}
