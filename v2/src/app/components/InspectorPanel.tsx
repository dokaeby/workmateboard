/**
 * Inspector Panel — spec 013 US1 (가이드 §7-D).
 * appStore.inspector가 설정되면 우측에 380px 상세 패널을 연다.
 * 데스크톱: flex row의 오른쪽 sibling. 모바일(<lg): 전체 화면 오버레이.
 * kind별 데이터:
 *  - run/trace → GET /runs/{id} (+ trace는 /runs/{id}/timeline, lead+)
 *  - agent/session → GET /agent-sessions/{key}/history + 세션 목록에서 상태 조회
 * read-only. 403은 Empty forbidden, 404는 Empty not-found로 수렴.
 */
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../../shared/api/client";
import type {
  AgentSession, RunDetail, RunTimelineResponse, SessionHistoryResponse, StageRun,
} from "../../shared/api/types";
import { useAppStore, type InspectorTarget } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { fmtTs, fmtElapsed, jsonSummary } from "../../shared/lib/format";
import { maskSecrets } from "../../shared/lib/redact";
import { Badge, Button, Card, Empty, ErrorBox, Spinner } from "../../shared/ui";

function statusTone(status: string | undefined | null): "ok" | "warn" | "err" | "accent" | "muted" {
  const s = String(status || "").toLowerCase();
  if (["succeeded", "success", "completed", "done", "ended", "ok"].includes(s)) return "ok";
  if (["failed", "error", "cancelled", "canceled", "rejected", "blocked"].includes(s)) return "err";
  if (["waiting_approval", "decision", "pending", "running", "working", "queued", "rest"].includes(s)) return "warn";
  if (["active", "started", "in_progress"].includes(s)) return "accent";
  return "muted";
}

function KindBadge({ kind }: { kind: InspectorTarget["kind"] }) {
  const { t } = useI18n();
  const label = { run: t("insp.kind.run", "실행"), agent: t("insp.kind.agent", "에이전트"), session: t("insp.kind.session", "세션"), trace: t("insp.kind.trace", "트레이스") }[kind];
  return <Badge tone="accent">{label}</Badge>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <span className="shrink-0 text-xs text-muted">{label}</span>
      <span className="min-w-0 truncate text-right font-mono text-xs text-ink-2" title={typeof children === "string" ? children : undefined}>{children}</span>
    </div>
  );
}

function DetailState({ q }: { q: { isLoading: boolean; isError: boolean; error: unknown; refetch: () => void } }) {
  const { t } = useI18n();
  if (q.isLoading) return <Spinner />;
  if (q.isError) {
    const e = q.error;
    if (e instanceof ApiError && e.status === 403) {
      return <Empty>{t("insp.forbidden", "이 정보를 볼 권한이 없습니다 (lead 이상 필요)")}</Empty>;
    }
    if (e instanceof ApiError && e.status === 404) {
      return <Empty>{t("insp.notFound", "항목을 찾을 수 없습니다")}</Empty>;
    }
    return <ErrorBox message={e instanceof Error ? e.message : String(e)} onRetry={() => void q.refetch()} />;
  }
  return null;
}

function StageList({ stages }: { stages: StageRun[] | undefined }) {
  const { t } = useI18n();
  if (!stages || stages.length === 0) return <Empty>{t("insp.noStages", "stage 정보가 없습니다")}</Empty>;
  return (
    <ul className="space-y-1">
      {stages.map((s, i) => {
        const key = s.stage_key || s.stage || `#${i}`;
        const dur = s.started_at ? fmtElapsed(s.started_at, s.finished_at ?? null) : "-";
        return (
          <li key={s.id || `${key}-${i}`} className="flex items-center gap-2 rounded-md border border-border bg-bg/40 px-2 py-1.5 text-xs">
            <span className="min-w-0 flex-1 truncate font-mono text-ink-2">{key}{s.attempt && s.attempt > 1 ? ` ×${s.attempt}` : ""}</span>
            <span className="text-muted">{dur}</span>
            <Badge tone={statusTone(s.status)}>{s.status || "-"}</Badge>
          </li>
        );
      })}
    </ul>
  );
}

function RunInspector({ id, trace }: { id: string; trace: boolean }) {
  const { t } = useI18n();
  const detailQ = useQuery({
    queryKey: ["inspector", "run", id],
    queryFn: () => apiGet<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  });
  const timelineQ = useQuery({
    queryKey: ["inspector", "run-timeline", id],
    queryFn: () => apiGet<RunTimelineResponse>(`/runs/${encodeURIComponent(id)}/timeline`),
    enabled: trace,
    retry: false,
  });
  const state = DetailState({ q: detailQ });
  if (state) return state;
  const d = detailQ.data;
  if (!d) return null;
  const timelineStages = timelineQ.data?.stages;
  return (
    <div className="space-y-3">
      <Card title={t("insp.summary", "요약")}>
        <Field label={t("insp.status", "상태")}><Badge tone={statusTone(d.status)}>{d.status || "-"}</Badge></Field>
        <Field label={t("insp.workflow", "워크플로")}>{d.workflow || "-"}</Field>
        <Field label={t("insp.created", "생성")}>{fmtTs(d.created_at)}</Field>
        <Field label={t("insp.updated", "갱신")}>{fmtTs(d.updated_at)}</Field>
        {d.project ? <Field label={t("insp.project", "프로젝트")}>{d.project}</Field> : null}
        {d.agent_id ? <Field label={t("insp.agent", "에이전트")}>{d.agent_id}</Field> : null}
        {d.trace_id ? <Field label={t("insp.traceId", "Trace ID")}>{d.trace_id}</Field> : null}
        {d.error ? <Field label={t("insp.error", "오류")}>{maskSecrets(String(d.error))}</Field> : null}
      </Card>
      {(d.approvals?.length ?? 0) > 0 && (
        <Card title={t("insp.approvals", "승인")}>
          <ul className="space-y-1 text-xs">
            {d.approvals!.map((a, i) => (
              <li key={i} className="flex items-center gap-2 text-ink-2">
                <span className="font-mono">{a.stage_key || "-"}</span>
                <Badge tone={statusTone(a.decision)}>{a.decision || "-"}</Badge>
                <span className="truncate text-muted">{a.decided_by || ""}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card title={trace ? t("insp.timeline", "타임라인") : t("insp.stages", "Stage")}>
        {trace && timelineQ.isLoading ? <Spinner /> : null}
        {trace && timelineQ.isError ? (
          timelineQ.error instanceof ApiError && timelineQ.error.status === 403
            ? <Empty>{t("insp.forbidden", "이 정보를 볼 권한이 없습니다 (lead 이상 필요)")}</Empty>
            : <ErrorBox message={timelineQ.error instanceof Error ? timelineQ.error.message : String(timelineQ.error)} onRetry={() => void timelineQ.refetch()} />
        ) : null}
        {/* timeline 성공+stage 있음 → 타임라인 표시, 그 외(비-trace/빈 결과)는 run 상세 stage로 폴백 */}
        {trace && timelineQ.isSuccess && timelineStages && timelineStages.length > 0 ? (
          <ul className="space-y-1">
            {timelineStages.slice(0, 50).map((s, i) => (
              <li key={i} className="flex items-center gap-2 rounded-md border border-border bg-bg/40 px-2 py-1.5 text-xs">
                <span className="min-w-0 flex-1 truncate font-mono text-ink-2">
                  {String(s.stage_key || s.stage || s.name || `#${i}`)}
                </span>
                <Badge tone={statusTone(String(s.status || ""))}>{String(s.status || "-")}</Badge>
              </li>
            ))}
          </ul>
        ) : !trace || (timelineQ.isSuccess && (!timelineStages || timelineStages.length === 0)) ? (
          <StageList stages={d.stages} />
        ) : null}
      </Card>
      {d.input ? (
        <Card title={t("insp.input", "입력")}>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-ink-2">{maskSecrets(jsonSummary(d.input, 2000))}</pre>
        </Card>
      ) : null}
    </div>
  );
}

function SessionInspector({ id }: { id: string }) {
  const { t } = useI18n();
  const histQ = useQuery({
    queryKey: ["inspector", "session-history", id],
    queryFn: () => apiGet<SessionHistoryResponse>(`/agent-sessions/${encodeURIComponent(id)}/history?limit=50`),
  });
  // 세션 상태/doing/tokens는 목록 엔드포인트에서 session_key로 역조회
  const listQ = useQuery({
    queryKey: ["inspector", "sessions-all"],
    queryFn: () => apiGet<AgentSession[]>("/agent-sessions?limit=500&include_ended=true"),
    staleTime: 15000,
  });
  const state = DetailState({ q: histQ });
  if (state) return state;
  const h = histQ.data;
  const sess = (listQ.data || []).find((s) => s.session_key === id);
  const tl = h?.timeline || [];
  return (
    <div className="space-y-3">
      <Card title={t("insp.summary", "요약")}>
        {sess ? (
          <>
            <Field label={t("insp.status", "상태")}><Badge tone={statusTone(sess.status)}>{sess.status || "-"}</Badge></Field>
            <Field label={t("insp.tool", "도구")}>{sess.tool || "-"}</Field>
            {sess.doing ? <Field label={t("insp.doing", "수행 중")}>{maskSecrets(sess.doing)}</Field> : null}
            {sess.ask ? <Field label={t("insp.ask", "질문")}>{maskSecrets(sess.ask)}</Field> : null}
            {sess.project ? <Field label={t("insp.project", "프로젝트")}>{sess.project}</Field> : null}
            <Field label={t("insp.tokens", "토큰")}>{`${sess.tokens_in ?? 0} / ${sess.tokens_out ?? 0}`}</Field>
            <Field label={t("insp.lastEvent", "마지막 이벤트")}>{fmtTs(sess.last_event_at || sess.updated_at)}</Field>
          </>
        ) : (
          <Empty>{t("insp.sessionMetaMissing", "세션 메타를 찾지 못했습니다 (종료된 오래된 세션일 수 있음)")}</Empty>
        )}
      </Card>
      {h?.summary ? (
        <Card title={t("insp.summaryText", "회고 요약")}>
          <p className="text-sm text-ink-2">{maskSecrets(h.summary)}</p>
        </Card>
      ) : null}
      <Card title={t("insp.timeline", "타임라인")}>
        {tl.length === 0 ? <Empty>{t("insp.noEvents", "이벤트가 없습니다")}</Empty> : (
          <ul className="space-y-1">
            {tl.slice(-30).map((e, i) => (
              <li key={i} className="flex items-start gap-2 rounded-md border border-border bg-bg/40 px-2 py-1.5 text-xs">
                <span className="shrink-0 font-mono text-muted">{fmtTs(e.at)}</span>
                <span className="min-w-0 flex-1">
                  <Badge tone={statusTone(e.to_state)}>{e.to_state || "-"}</Badge>
                  {e.text ? <span className="ml-1.5 text-ink-2">{e.text}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export function InspectorPanel() {
  const inspector = useAppStore((s) => s.inspector);
  const setInspector = useAppStore((s) => s.setInspector);
  const { t } = useI18n();

  // ESC로 닫기
  useEffect(() => {
    if (!inspector) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setInspector(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [inspector, setInspector]);

  if (!inspector) return null;
  const title = {
    run: t("insp.title.run", "실행 상세"),
    agent: t("insp.title.agent", "에이전트 상세"),
    session: t("insp.title.session", "세션 상세"),
    trace: t("insp.title.trace", "트레이스 상세"),
  }[inspector.kind];

  return (
    <aside
      className="fixed inset-0 z-40 flex flex-col bg-surface lg:static lg:z-auto lg:w-[380px] lg:shrink-0 lg:border-l lg:border-border"
      role="complementary"
      aria-label={title}
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2.5">
        <KindBadge kind={inspector.kind} />
        <h2 className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">{title}</h2>
        <span className="max-w-32 truncate font-mono text-[11px] text-muted" title={inspector.id}>{inspector.id}</span>
        <Button variant="ghost" className="px-2" onClick={() => setInspector(null)} aria-label={t("insp.close", "닫기")}>
          ✕
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {inspector.kind === "run" || inspector.kind === "trace" ? (
          <RunInspector id={inspector.id} trace={inspector.kind === "trace"} />
        ) : (
          <SessionInspector id={inspector.id} />
        )}
      </div>
    </aside>
  );
}
