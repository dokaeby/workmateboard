/** 세션 기록 — GET /agent-sessions 목록 + GET .../history 타임라인/회고.
 * 레거시 index.html의 loadSessionlog/openSessionHistory/sessionHistoryHtml을 이행. */
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, ApiError } from "../../shared/api/client";
import type { AgentSession } from "../../shared/api/types";
import { STR } from "./i18n";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, usd } from "../../shared/lib/format";

registerI18n(STR);

// 레거시 SESSION_TOOL_LABEL
const TOOL_LABEL: Record<string, string> = {
  claude: "Claude", codex: "Codex", gemini: "Gemini", devin: "Devin",
};

interface SessionRow extends AgentSession {
  health?: string;
  est_cost_usd?: number | null;
  ended_summary?: string;
  created_at?: string;
  event_at?: string;
}

interface TimelineItem {
  at?: string;
  from_state?: string;
  to_state?: string;
  text?: string;
  duration_s?: number | null;
}
interface SessionStats {
  state_counts?: Record<string, number>;
  decisions?: number;
  approvals?: number;
  errors?: number;
  tokens_in?: number;
  tokens_out?: number;
  est_cost_usd?: number | null;
  total_seconds?: number;
}
interface SessionHistory {
  session?: SessionRow;
  events?: unknown[];
  timeline?: TimelineItem[];
  stats?: SessionStats;
  files_touched?: unknown;
  summary?: string;
  ended_summary?: string;
}
interface ProjectItem { id?: string; name?: string }
interface ProjectsResponse { items?: ProjectItem[] }

function toolLabel(tool: string | undefined): string {
  return TOOL_LABEL[String(tool || "")] || String(tool || "-");
}

function statusTone(status: string | undefined): "muted" | "warn" | "accent" | "err" {
  const s = String(status || "");
  if (s === "ended") return "muted";
  if (s === "decision") return "err";
  if (s === "ask" || s === "blocked" || s === "error") return "warn";
  if (s === "working" || s === "running") return "accent";
  return "muted";
}

// 레거시 officeFmtSecs 대응 — 초 → "N시간 N분 N초" (단위는 t() 로 언어 전환)
function fmtDurSec(sec: number | null | undefined, t: (k: string, f?: string) => string): string {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return "-";
  const total = Math.floor(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const r = total % 60;
  if (h > 0) return `${h}${t("sl.durH", "시간")} ${m}${t("sl.durM", "분")}`;
  if (m > 0) return `${m}${t("sl.durM", "분")} ${r}${t("sl.durS", "초")}`;
  return `${r}${t("sl.durS", "초")}`;
}

function filesList(files: unknown): string[] {
  if (Array.isArray(files)) return files.map((f) => (typeof f === "object" && f ? String((f as { path?: string }).path || JSON.stringify(f)) : String(f)));
  if (files && typeof files === "object") return Object.keys(files as Record<string, unknown>);
  return [];
}

export default function SessionlogPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<SessionRow | null>(null);

  const sessionsQ = useQuery({
    queryKey: ["agent-sessions"],
    queryFn: () => apiGet<SessionRow[] | { sessions?: SessionRow[]; agent_sessions?: SessionRow[] }>(
      "/agent-sessions?limit=100&include_ended=true",
    ),
    refetchInterval: 30000,
  });
  const raw = sessionsQ.data;
  const totalCount = useMemo(() => {
    const list = Array.isArray(raw) ? raw : (raw?.sessions || raw?.agent_sessions || []);
    return list.length;
  }, [raw]);
  const sessions = useMemo(() => {
    const list = Array.isArray(raw) ? raw : (raw?.sessions || raw?.agent_sessions || []);
    const needle = q.trim().toLowerCase();
    if (!needle) return list;
    return list.filter((s) =>
      [s.tool, s.project, s.session_key, s.title, s.doing, s.ask]
        .some((v) => String(v || "").toLowerCase().includes(needle)),
    );
  }, [raw, q]);

  // 프로젝트 태그 후보 — lead 전용이라 실패 시 빈 목록 (셀렉트만 현재값 표시)
  const projectsQ = useQuery({
    queryKey: ["projects-master"],
    queryFn: () => apiGet<ProjectsResponse>("/projects"),
    retry: false,
    staleTime: 60000,
  });
  const projectNames = useMemo(
    () => [...new Set(((projectsQ.data?.items || []).map((p) => p.name).filter(Boolean) as string[]))],
    [projectsQ.data],
  );

  const histQ = useQuery({
    queryKey: ["session-history", sel?.session_key],
    enabled: !!sel?.session_key,
    queryFn: async () => {
      const key = encodeURIComponent(String(sel?.session_key));
      try {
        // 레거시와 동일 — session_key 단독 경로 우선, 실패 시 tool 한정 경로
        return await apiGet<SessionHistory>(`/agent-sessions/${key}/history`);
      } catch {
        return apiGet<SessionHistory>(
          `/agent-sessions/${encodeURIComponent(String(sel?.tool || ""))}/${key}/history`,
        );
      }
    },
  });

  const setProject = async (name: string) => {
    if (!sel?.session_key) return;
    try {
      await apiPatch(`/agent-sessions/${encodeURIComponent(String(sel.session_key))}`, {
        project: name || null,
      });
      qc.invalidateQueries({ queryKey: ["agent-sessions"] });
      setSel({ ...sel, project: name || "" });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  const hist = histQ.data;
  const stats = hist?.stats || {};
  const timeline = (hist?.timeline || []).slice().reverse(); // 최신 먼저
  const files = filesList(hist?.files_touched);
  const curProject = sel?.project || "";
  const names = [...new Set([...projectNames, ...(curProject ? [curProject] : [])])];

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead title={t("sl.title", "세션 기록")} desc={t("sl.desc", "에이전트 세션 목록과 상태 전이 타임라인")}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("sl.search", "검색")}
          className="w-64 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-muted"
        />
        <Button variant="secondary" className="text-xs" onClick={() => sessionsQ.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        {/* 세션 목록 */}
        <Card title={t("sl.title", "세션 기록")}>
          {sessionsQ.isLoading && <Spinner />}
          {sessionsQ.error && (
            <ErrorBox
              message={sessionsQ.error instanceof Error ? sessionsQ.error.message : String(sessionsQ.error)}
              onRetry={() => sessionsQ.refetch()}
            />
          )}
          {sessionsQ.data && !sessions.length && (
            <Empty>{totalCount === 0 ? t("sl.empty", "세션이 없습니다") : t("sl.noResult", "검색 결과가 없습니다")}</Empty>
          )}
          <div className="divide-y divide-border">
            {sessions.map((s) => {
              const key = `${s.tool}:${s.session_key}`;
              const active = sel?.session_key === s.session_key && sel?.tool === s.tool;
              const label = `${toolLabel(s.tool)} · ${s.title || s.project || s.session_key}`;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSel(s)}
                  className={`block w-full px-2 py-2.5 text-left transition-colors hover:bg-surface-hover ${
                    active ? "bg-accent-soft/50" : ""
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{label}</span>
                    <Badge tone={statusTone(s.status)}>{s.status || "-"}</Badge>
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                    {s.project ? <span className="rounded bg-surface-hover px-1">{s.project}</span> : null}
                    <span className="truncate">{s.doing || s.ask || s.ended_summary || ""}</span>
                    {s.est_cost_usd != null ? <span className="shrink-0">{usd(s.est_cost_usd)}</span> : null}
                  </div>
                  <div className="mt-0.5 text-xs text-muted">
                    {fmtTs(s.updated_at || s.event_at || s.last_event_at || s.created_at)}
                  </div>
                </button>
              );
            })}
          </div>
        </Card>

        {/* 세션 상세 / 히스토리 */}
        <Card
          title={sel ? `${toolLabel(sel.tool)} · ${sel.title || sel.session_key}` : t("sl.detail", "세션 상세")}
          actions={sel ? (
            <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setSel(null)}>
              {t("sl.close", "닫기")}
            </Button>
          ) : undefined}
        >
          {!sel && <Empty>{t("sl.pick", "왼쪽 목록에서 세션을 선택하세요")}</Empty>}
          {sel && histQ.isLoading && <Spinner />}
          {sel && histQ.error && (
            <ErrorBox
              message={histQ.error instanceof Error ? histQ.error.message : String(histQ.error)}
              onRetry={() => histQ.refetch()}
            />
          )}
          {sel && hist && (
            <div className="space-y-3">
              {/* 프로젝트 태그 (lead 이상 — 실패 시 셀렉트 숨김) */}
              {projectsQ.data ? (
                <div className="flex items-center gap-2 text-xs text-muted">
                  <span>{t("sl.projectTag", "프로젝트 태그")}</span>
                  <select
                    value={curProject}
                    onChange={(e) => setProject(e.target.value)}
                    className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink"
                  >
                    <option value="">{t("sl.none", "(없음)")}</option>
                    {names.map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                </div>
              ) : null}

              {hist.summary ? (
                <div className="rounded-lg border border-border bg-surface-hover/50 p-3">
                  <div className="mb-1 text-xs font-bold text-muted">{t("sl.summary", "요약")}</div>
                  <div className="text-sm text-ink-2">{hist.summary}</div>
                </div>
              ) : null}

              {/* 통계 칩 */}
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(stats.state_counts || {}).map(([k, v]) => (
                  <Badge key={k} tone="muted">{k} {v}</Badge>
                ))}
                <Badge tone="accent">{t("sl.decisions", "결정")} {stats.decisions ?? 0}</Badge>
                <Badge tone="accent">{t("sl.approvals", "승인")} {stats.approvals ?? 0}</Badge>
                <Badge tone={(stats.errors ?? 0) > 0 ? "err" : "muted"}>
                  {t("sl.errors", "오류")} {stats.errors ?? 0}
                </Badge>
                <Badge tone="muted">
                  {t("sl.tokensIn", "토큰 in")} {(stats.tokens_in ?? 0).toLocaleString()} /{" "}
                  {t("sl.tokensOut", "토큰 out")} {(stats.tokens_out ?? 0).toLocaleString()}
                </Badge>
                <Badge tone="muted">{t("sl.cost", "비용")} {usd(stats.est_cost_usd)}</Badge>
                <Badge tone="muted">{t("sl.totalTime", "총 시간")} {fmtDurSec(stats.total_seconds, t)}</Badge>
              </div>

              {/* 상태 전이 타임라인 */}
              <div>
                <div className="mb-1 text-xs font-bold text-muted">{t("sl.timeline", "상태 전이")}</div>
                {timeline.length ? (
                  <ol className="space-y-1.5">
                    {timeline.map((tl, i) => {
                      const ended = tl.to_state === "ended" || tl.to_state === "end";
                      const trans = tl.from_state && tl.to_state
                        ? `${tl.from_state} → ${tl.to_state}`
                        : (tl.to_state || tl.from_state || "");
                      return (
                        <li
                          key={i}
                          className={`flex flex-wrap items-baseline gap-x-2 rounded-md px-2 py-1 text-sm ${
                            ended ? "bg-surface-hover text-muted" : "text-ink-2"
                          }`}
                        >
                          <span className="shrink-0 text-xs text-muted">{fmtTs(tl.at)}</span>
                          {trans ? <b className="text-xs">{trans}</b> : null}
                          {tl.text ? <span className="min-w-0 flex-1">{tl.text}</span> : null}
                          {tl.duration_s != null ? (
                            <span className="text-xs text-muted">({fmtDurSec(tl.duration_s, t)})</span>
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                ) : (
                  <div className="text-xs text-muted">{t("sl.noTimeline", "타임라인이 없습니다")}</div>
                )}
              </div>

              {files.length ? (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("sl.files", "건드린 파일")}</div>
                  <div className="flex flex-wrap gap-1">
                    {files.slice(0, 40).map((f, i) => (
                      <span key={i} className="rounded bg-surface-hover px-1.5 py-0.5 font-mono text-[11px] text-ink-2">
                        {f}
                      </span>
                    ))}
                    {files.length > 40 ? <span className="text-xs text-muted">{t("sl.others", "외 {n}개").replace("{n}", String(files.length - 40))}</span> : null}
                  </div>
                </div>
              ) : null}

              {hist.ended_summary || sel.ended_summary ? (
                <div className="text-xs text-muted">
                  {t("sl.endedSummary", "종료 요약")}: {hist.ended_summary || sel.ended_summary}
                </div>
              ) : null}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
