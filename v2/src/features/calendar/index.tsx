/** Calendar — 추출·등록된 일정 목록, 검토 필요 이벤트 승인/거부, 알림 예약. 레거시 loadCalendar 이식. */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";

const REVIEW_STATUSES = ["needs_review", "needs_confirmation", "candidate"] as const;

interface CalEvent {
  id: string;
  status?: string;
  reason?: string;
  title?: string;
  start_at?: string;
  end_at?: string;
  timezone?: string;
  location?: string;
  summary?: string;
  source_provider?: string;
  source_id?: string;
  calendar_event_id?: string;
}
interface NotifyJob {
  id?: string;
  kind?: string;
  status?: string;
  scheduled_at?: string;
  scheduled_event_id?: string;
}

const STATUS_TONE: Record<string, "ok" | "warn" | "err" | "accent" | "muted"> = {
  registered: "ok", succeeded: "ok",
  needs_review: "warn", needs_confirmation: "warn", candidate: "warn",
  rejected: "err", failed: "err",
};

function tone(status?: string) {
  return STATUS_TONE[status || ""] ?? "muted";
}

function ReviewCard({ ev }: { ev: CalEvent }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [title, setTitle] = useState(ev.title || "");
  const [startAt, setStartAt] = useState(ev.start_at || "");
  const [endAt, setEndAt] = useState(ev.end_at || "");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);

  const review = async (action: "approve" | "reject") => {
    setBusy(action);
    setMsg("");
    try {
      const body: Record<string, string> = { action };
      if (action === "approve") {
        body.title = title;
        body.start_at = startAt;
        body.end_at = endAt;
      }
      const r = await apiPost<{ calendar_event_id?: string }>(
        `/events/${encodeURIComponent(ev.id)}/review`, body);
      setMsg(action === "approve"
        ? `${t("cal.approved", "등록됨")} (${r.calendar_event_id || ""})`
        : t("cal.rejected", "거부됨"));
      setTimeout(() => qc.invalidateQueries({ queryKey: ["calendar"] }), 800);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const inputCls = "w-full rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink";
  return (
    <Card className="mb-3">
      <div className="mb-1 flex items-center gap-2">
        <Badge tone={tone(ev.status)}>{ev.status || "-"}</Badge>
        {ev.reason && <span className="font-mono text-[11px] text-muted">{ev.reason}</span>}
      </div>
      <div className="text-sm font-semibold text-ink">{ev.title}</div>
      <div className="mt-1 text-xs text-muted">
        {ev.start_at} ~ {ev.end_at} ({ev.timezone || ""})
      </div>
      <div className="mt-2 grid gap-1.5">
        <input className={inputCls} value={title} placeholder={t("cal.titlePh", "제목")}
          onChange={(e) => setTitle(e.target.value)} />
        <input className={inputCls} value={startAt} placeholder={t("cal.startPh", "시작 (ISO)")}
          onChange={(e) => setStartAt(e.target.value)} />
        <input className={inputCls} value={endAt} placeholder={t("cal.endPh", "종료 (ISO)")}
          onChange={(e) => setEndAt(e.target.value)} />
      </div>
      <div className="mt-2 flex items-center gap-2">
        <Button className="px-2.5 py-1 text-xs" disabled={busy !== null} onClick={() => review("approve")}>
          {t("cal.approve", "승인·등록")}
        </Button>
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy !== null}
          onClick={() => review("reject")}>
          {t("cal.reject", "거부")}
        </Button>
        {msg && <span className="text-xs text-muted" role="status">{msg}</span>}
      </div>
    </Card>
  );
}

export default function CalendarPage() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role);
  const qc = useQueryClient();

  const reviewQ = useQuery({
    queryKey: ["calendar-review"],
    queryFn: async (): Promise<CalEvent[]> => {
      const lists = await Promise.all(
        REVIEW_STATUSES.map((s) => apiGet<CalEvent[]>(`/events?status=${s}&limit=20`)));
      return lists.flat();
    },
    refetchInterval: 60000,
  });
  const eventsQ = useQuery({
    queryKey: ["calendar-events"],
    queryFn: () => apiGet<CalEvent[]>("/events?limit=50"),
    refetchInterval: 60000,
  });
  const jobsQ = useQuery({
    queryKey: ["calendar-jobs"],
    queryFn: () => apiGet<NotifyJob[]>("/notification-jobs?limit=20"),
    refetchInterval: 60000,
  });

  const reviewItems = reviewQ.data || [];
  const events = eventsQ.data || [];
  const jobs = jobsQ.data || [];
  const loading = eventsQ.isLoading || jobsQ.isLoading;
  const error = eventsQ.error || jobsQ.error;

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("cal.title", "Calendar")} desc={t("cal.desc", "추출·등록된 일정과 알림 예약")}>
        <Button variant="secondary" className="text-xs" onClick={() => {
          qc.invalidateQueries({ queryKey: ["calendar"] });
        }}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      {canLead && reviewItems.length > 0 && (
        <div className="mb-4">
          <h2 className="mb-2 text-sm font-semibold text-ink">
            {t("cal.reviewNeeded", "검토 필요")} ({reviewItems.length})
          </h2>
          {reviewQ.isLoading && <Spinner />}
          {reviewItems.map((ev) => <ReviewCard key={ev.id} ev={ev} />)}
        </div>
      )}
      {!canLead && reviewItems.length > 0 && (
        <div className="mb-3 text-xs text-muted">
          {t("cal.reviewLead", "검토 대기")}: {reviewItems.length} — {t("cal.needLead", "lead 이상 권한 필요")}
        </div>
      )}

      <Card title={t("cal.events", "일정")}>
        {loading && <Spinner />}
        {error && <ErrorBox message={(error as Error).message} onRetry={() => { eventsQ.refetch(); jobsQ.refetch(); }} />}
        {!loading && !error && !events.length && <Empty>{t("cal.noEvents", "등록된 일정 없음")}</Empty>}
        <div className="grid gap-3">
          {events.map((ev) => (
            <Card key={ev.id}>
              <div className="mb-1 flex items-center gap-2">
                <Badge tone={tone(ev.status)}>{ev.status || "-"}</Badge>
                {ev.source_provider && (
                  <span className="font-mono text-[11px] text-muted">{ev.source_provider}:{ev.source_id}</span>
                )}
              </div>
              <div className="text-sm font-semibold text-ink">{ev.title}</div>
              <div className="mt-1 text-xs text-muted">
                {ev.start_at} ~ {ev.end_at}{ev.location ? ` · ${ev.location}` : ""}
              </div>
              {ev.summary && <div className="mt-1 text-sm text-ink-2">{ev.summary}</div>}
              {ev.calendar_event_id && (
                <div className="mt-1 text-[11px] text-muted">event: {ev.calendar_event_id}</div>
              )}
            </Card>
          ))}
        </div>
        {jobs.length > 0 && (
          <>
            <h3 className="mb-2 mt-5 text-sm font-semibold text-ink">{t("cal.notifyJobs", "알림 예약")}</h3>
            <div className="grid gap-3">
              {jobs.map((j, i) => (
                <Card key={j.id || i}>
                  <div className="mb-1 flex items-center gap-2">
                    <Badge tone={tone(j.status || "scheduled")}>{j.status || "scheduled"}</Badge>
                  </div>
                  <div className="text-sm font-semibold text-ink">{j.kind || ""} {t("cal.notify", "알림")}</div>
                  <div className="mt-1 font-mono text-xs text-muted">
                    {j.scheduled_at || ""} · {j.scheduled_event_id || ""}
                  </div>
                </Card>
              ))}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
