/** Gmail Checker — 이메일 요약 카드 + 다이제스트 + gmail-calendar 폴링 상태/제어. 레거시 loadGmail 이식. */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, safeUrl } from "../../shared/lib/format";
import { STR } from "./i18n";

registerI18n(STR);

interface SummaryCard {
  source_id?: string;
  subject?: string;
  sender?: string;
  summary?: string;
  message_at?: string;
  checked_at?: string;
  url?: string;
}
interface GmailMessage {
  source_provider?: string;
  source_id?: string;
  source_text?: string;
  processing_status?: string;
  created_at?: string;
  error_message?: string;
  extraction?: { event?: { title?: string; start_at?: string } };
}
interface GmailStatus {
  enabled?: boolean;
  last_poll_at?: string | null;
  last_poll_result?: string;
}
interface InboxData { cards: SummaryCard[]; messages: GmailMessage[] }

function SummaryItem({ c }: { c: SummaryCard }) {
  const { t } = useI18n();
  const dt = (c.message_at || c.checked_at || "").slice(0, 16).replace("T", " ");
  return (
    <Card>
      <div className="mb-1 flex items-center gap-2">
        <Badge tone="accent">{t("gmail.summaryBadge", "요약")}</Badge>
        <span className="text-xs text-muted">{dt}</span>
        <a href={safeUrl(c.url)} target="_blank" rel="noopener noreferrer"
          className="ml-auto text-xs text-accent hover:underline">
          Gmail ↗
        </a>
      </div>
      <div className="text-sm font-semibold text-ink">{c.subject || t("gmail.noSubject", "(제목 없음)")}</div>
      {c.sender && <div className="mt-0.5 text-xs text-muted">{c.sender}</div>}
      {c.summary && <div className="mt-1 text-sm text-ink-2">{c.summary}</div>}
    </Card>
  );
}

function MessageItem({ m, canLead, onRetry, busy }: {
  m: GmailMessage; canLead: boolean; busy: boolean;
  onRetry: (provider: string, sid: string) => void;
}) {
  const { t } = useI18n();
  const st = m.processing_status || "";
  const tone = st === "processed" ? "ok" : st === "failed" ? "err" : st === "pending" ? "warn" : "muted";
  const lines = (m.source_text || "").split("\n").filter((x) => x.trim());
  const title = lines[0] || m.source_id || "";
  const body = lines.slice(1).join("\n").slice(0, 400);
  const retryable = st === "pending" || st === "failed";
  return (
    <Card>
      <div className="mb-1 flex items-center gap-2">
        <Badge tone={tone}>{st || "-"}</Badge>
        <span className="text-xs text-muted">{fmtTs(m.created_at)}</span>
        {retryable && canLead && m.source_provider && m.source_id && (
          <Button variant="secondary" className="ml-auto px-2 py-0.5 text-[11px]" disabled={busy}
            onClick={() => onRetry(m.source_provider!, m.source_id!)}>
            {t("gmail.retry", "재시도")}
          </Button>
        )}
      </div>
      <div className="text-sm font-semibold text-ink">{title}</div>
      {body && <div className="mt-1 whitespace-pre-wrap text-sm text-ink-2">{body}</div>}
      {m.extraction?.event && (
        <div className="mt-1 text-xs text-muted">
          {t("gmail.extracted", "추출")}: {m.extraction.event.title || ""} {m.extraction.event.start_at || ""}
        </div>
      )}
      {m.error_message && <div className="mt-1 text-xs text-err">{m.error_message}</div>}
    </Card>
  );
}

export default function GmailPage() {
  const { t } = useI18n();
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role);
  const qc = useQueryClient();
  const [digest, setDigest] = useState("");
  const [busyPoll, setBusyPoll] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const digestTried = useRef(false);

  const inboxQ = useQuery({
    queryKey: ["gmail-inbox"],
    queryFn: async (): Promise<InboxData> => {
      try {
        const d = await apiGet<{ cards?: SummaryCard[] }>("/email/summaries?limit=30");
        const cards = d.cards || [];
        if (cards.length) return { cards, messages: [] };
      } catch { /* summaries 없으면 messages 폴백 */ }
      const d = await apiGet<{ messages?: GmailMessage[] }>("/messages?provider=gmail&limit=30");
      return { cards: [], messages: d.messages || [] };
    },
    refetchInterval: 60000,
  });

  const statusQ = useQuery({
    queryKey: ["gmail-calendar-status"],
    queryFn: () => apiGet<GmailStatus>("/gmail-calendar"),
    refetchInterval: 60000,
  });

  // 다이제스트는 세션당 1회만 생성 시도 (lead 전용, 일일 상한 있음)
  useEffect(() => {
    if (digestTried.current || !canLead) return;
    digestTried.current = true;
    apiPost<{ summary?: string }>("/gmail/digest", {})
      .then((d) => { if (d.summary) setDigest(d.summary); })
      .catch(() => { /* 다이제스트 실패는 무시 */ });
  }, [canLead]);

  const toggle = async () => {
    const cur = !!statusQ.data?.enabled;
    try {
      await apiPost("/gmail-calendar/toggle", { enabled: !cur });
      qc.invalidateQueries({ queryKey: ["gmail-calendar-status"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };
  const poll = async () => {
    setBusyPoll(true);
    try {
      await apiPost("/gmail-calendar/poll", {});
      setTimeout(() => qc.invalidateQueries({ queryKey: ["gmail-calendar-status"] }), 3000);
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusyPoll(false);
    }
  };
  const retry = async (provider: string, sid: string) => {
    const key = `${provider}:${sid}`;
    setRetrying(key);
    try {
      await apiPost(`/messages/${encodeURIComponent(provider)}/${encodeURIComponent(sid)}/retry`, {});
      qc.invalidateQueries({ queryKey: ["gmail-inbox"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRetrying(null);
    }
  };

  const cards = inboxQ.data?.cards || [];
  const messages = inboxQ.data?.messages || [];
  const empty = !inboxQ.isLoading && !cards.length && !messages.length;

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("gmail.title", "Gmail Checker")} desc={t("gmail.desc", "수신함 메시지 요약 및 일정 추출 상태")}>
        {canLead && (
          <>
            <Button variant="secondary" className="text-xs" disabled={busyPoll} onClick={poll}>
              {busyPoll ? t("gmail.polling", "폴링 중…") : t("gmail.pollNow", "지금 폴링")}
            </Button>
            <Button variant={statusQ.data?.enabled ? "danger" : "primary"} className="text-xs" onClick={toggle}>
              {statusQ.data?.enabled ? t("gmail.autoOff", "자동 폴링 끄기") : t("gmail.autoOn", "자동 폴링 켜기")}
            </Button>
          </>
        )}
        <Button variant="secondary" className="text-xs" onClick={() => inboxQ.refetch()}>
          {t("common.refresh", "새로고침")}
        </Button>
      </PageHead>

      <Card className="mb-3" title={t("gmail.pollStatus", "Gmail → Calendar 폴링")}>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <Badge tone={statusQ.data?.enabled ? "ok" : "muted"}>
            {statusQ.data?.enabled ? t("gmail.on", "켜짐") : t("gmail.off", "꺼짐")}
          </Badge>
          <span>{t("gmail.lastPoll", "마지막 폴링")}: {statusQ.data?.last_poll_at ? fmtTs(statusQ.data.last_poll_at) : "-"}</span>
          {statusQ.data?.last_poll_result && <span className="truncate">· {statusQ.data.last_poll_result}</span>}
        </div>
      </Card>

      {digest && (
        <Card title={t("gmail.digestTitle", "오늘의 요약")} className="mb-3">
          <pre className="whitespace-pre-wrap text-sm text-ink-2">{digest}</pre>
        </Card>
      )}

      {inboxQ.isLoading && <Spinner />}
      {inboxQ.error && <ErrorBox message={(inboxQ.error as Error).message} onRetry={() => inboxQ.refetch()} />}
      {empty && <Empty>{t("gmail.empty", "메시지 없음")}</Empty>}

      <div className="grid gap-3">
        {cards.map((c, i) => <SummaryItem key={c.source_id || i} c={c} />)}
        {messages.map((m, i) => (
          <MessageItem key={`${m.source_provider}:${m.source_id}:${i}`} m={m} canLead={canLead}
            busy={retrying === `${m.source_provider}:${m.source_id}`}
            onRetry={retry} />
        ))}
      </div>
    </div>
  );
}
