/** 관리 에이전트 로스터 — /management/agents 실데이터 (paperclip spec 1차 F항).
 *
 * 에이전트 이름/role/status/현재 task/마지막 heartbeat를 10초 주기로 표시한다.
 * 상태 매핑: running→작업 중(ok), pending_approval→결정 필요(warn),
 * active/idle→대기, paused→일시정지 배지, error→오류, terminated는
 * 기본 숨김(토글로 표시). 실제 agents가 없으면 빈 상태만 보여주고
 * 데모 데이터는 섞지 않는다 — 데모는 기존 스프라이트 토글 전용. */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Card, Empty, Spinner, ErrorBox, Badge } from "../../shared/ui";
import { fmtAge } from "../../shared/lib/format";

interface MgmtTask {
  id?: string;
  identifier?: string;
  title?: string;
  status?: string;
  priority?: string;
}

interface MgmtAgent {
  id: string;
  name?: string;
  role?: string;
  title?: string;
  status?: string;
  kind?: string;
  last_seen_at?: string | null;
  current_task?: MgmtTask | null;
}

interface MgmtAgentsResponse {
  items?: MgmtAgent[];
}

const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  running: "ok",
  active: "ok",
  idle: "muted",
  pending_approval: "warn",
  paused: "warn",
  error: "err",
  terminated: "muted",
};

const TASK_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  in_progress: "accent",
  in_review: "warn",
  waiting_approval: "warn",
  blocked: "err",
  done: "ok",
};

const PRIORITY_TONE: Record<string, "ok" | "warn" | "err" | "muted" | "accent"> = {
  urgent: "err",
  high: "warn",
  medium: "muted",
  low: "muted",
};

function statusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(`office.mgmt.st.${st}`, st);
}

function taskStatusLabel(t: (k: string, f?: string) => string, st: string): string {
  return t(`office.mgmt.taskStatus.${st}`, st);
}

function priorityLabel(t: (k: string, f?: string) => string, p: string): string {
  return t(`office.mgmt.priority.${p}`, p);
}

function lastSeenLabel(
  t: (k: string, f?: string) => string,
  iso: string | null | undefined,
): string {
  if (!iso) return t("office.mgmt.neverSeen", "no signal");
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return t("office.mgmt.neverSeen", "no signal");
  return fmtAge(ms / 1000);
}

export function ManagementRoster() {
  const { t } = useI18n();
  const [showTerminated, setShowTerminated] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["mgmt-agents"],
    queryFn: () => apiGet<MgmtAgentsResponse>("/management/agents"),
    refetchInterval: 10000,
  });

  const agents = useMemo(() => {
    const items = Array.isArray(data?.items) ? data.items : [];
    return items.filter((a) => showTerminated || (a.status || "") !== "terminated");
  }, [data, showTerminated]);

  return (
    <Card title={t("office.mgmt.title", "관리 에이전트")}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[11px] text-muted">
          {agents.length} · {t("office.mgmt.lastSeen", "마지막 신호")}
        </span>
        <label className="flex items-center gap-1.5 text-[11px] text-muted">
          <input
            type="checkbox"
            checked={showTerminated}
            onChange={(e) => setShowTerminated(e.target.checked)}
          />
          {t("office.mgmt.showTerminated", "종료된 에이전트 포함")}
        </label>
      </div>
      {isLoading && <Spinner />}
      {error && (
        <ErrorBox
          message={error instanceof Error ? error.message : String(error)}
          onRetry={() => refetch()}
        />
      )}
      {data && !agents.length && (
        <Empty>{t("office.mgmt.empty", "등록된 관리 에이전트가 없습니다")}</Empty>
      )}
      <ul className="space-y-1.5">
        {agents.map((a) => {
          const st = String(a.status || "active").toLowerCase();
          const ct = a.current_task;
          return (
            <li
              key={a.id}
              className="flex items-center gap-2 rounded-md border border-border bg-surface-hover px-2 py-1.5"
            >
              <Badge tone={STATUS_TONE[st] || "muted"}>
                {statusLabel(t, st)}
              </Badge>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-xs font-medium text-ink">
                    {a.name || a.id}
                  </span>
                  {a.kind && a.kind !== "workflow" && (
                    <span className="text-[10px] text-muted">
                      {t(`office.mgmt.kind.${a.kind}`, a.kind)}
                    </span>
                  )}
                </div>
                <div className="truncate text-[11px] text-muted">
                  {a.role || a.title || ""}
                  {ct ? (
                    <>
                      {" · "}
                      <span className="font-medium text-ink">
                        {ct.identifier || ct.id}
                      </span>{" "}
                      {ct.title}
                    </>
                  ) : (
                    <> · {t("office.mgmt.noTask", "대기 중인 작업 없음")}</>
                  )}
                </div>
              </div>
              <div className="shrink-0 text-right text-[10px] text-muted">
                <div>{lastSeenLabel(t, a.last_seen_at)}</div>
                {ct?.status && (
                  <Badge tone={TASK_TONE[String(ct.status)] || "muted"}>
                    {taskStatusLabel(t, String(ct.status))}
                  </Badge>
                )}
                {ct?.priority && (
                  <Badge tone={PRIORITY_TONE[String(ct.priority)] || "muted"}>
                    {priorityLabel(t, String(ct.priority))}
                  </Badge>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
