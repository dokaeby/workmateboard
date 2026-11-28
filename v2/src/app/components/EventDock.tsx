/**
 * Bottom Event Dock — spec 013 US2 (가이드 §7-E).
 * 화면 하단 접이식 실시간 이벤트 피드. 터미널 스타일(font-mono).
 * 소스: ① SSE /stream 이벤트(run/stage/session — 기존 단일 연결의
 * 이벤트 버스 재사용, snapshot 제외) ② GET /observability/external-logs
 * (lead+ — 403이면 조용히 SSE만 표시). 시간순 머지, 최대 500행.
 * 레벨 필터(ALL/INFO/WARN/ERROR), 스크롤 업 시 자동 스크롤 일시정지,
 * 비밀값은 표시 전 maskSecrets로 마스킹. 접힘 상태는 localStorage.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import type { ExternalLog, ExternalLogsResponse } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import { useSseEvents } from "../../shared/stream";
import { maskSecrets } from "../../shared/lib/redact";
import { fmtTs } from "../../shared/lib/format";

const MAX_ENTRIES = 500;
const COLLAPSED_KEY = "jm_dock_collapsed";

type Level = "INFO" | "WARN" | "ERROR";
type Filter = "ALL" | Level;

interface DockEntry {
  id: string;
  ts: number;       // epoch ms — 정렬/표시 기준
  level: Level;
  source: string;   // "run" | "stage" | "session" | "ext-log"
  message: string;
}

const BAD_STATUS = new Set(["failed", "error", "cancelled", "canceled", "rejected", "blocked"]);
const WARN_STATUS = new Set(["waiting_approval", "decision", "pending", "queued", "rest"]);

function levelOf(status: unknown): Level {
  const s = String(status ?? "").toLowerCase();
  if (BAD_STATUS.has(s)) return "ERROR";
  if (WARN_STATUS.has(s)) return "WARN";
  return "INFO";
}

function logLevel(log: ExternalLog): Level {
  const num = Number(log.severity_number);
  const text = `${log.severity_text ?? ""} ${log.severity ?? ""}`.toUpperCase();
  if (num >= 17 || text.includes("ERROR") || text.includes("FATAL")) return "ERROR";
  if (num >= 13 || text.includes("WARN")) return "WARN";
  return "INFO";
}

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

/** SSE 이벤트 → 피드 엔트리. snapshot은 전체 스냅샷이라 피드에 넣지 않는다. */
function sseToEntries(event: string, data: unknown): DockEntry[] {
  const d = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  const now = Date.now();
  if (event === "run") {
    const id = str(d.id);
    if (!id) return [];
    return [{
      id: `run:${id}:${str(d.updated_at) || now}`,
      ts: now, level: levelOf(d.status), source: "run",
      message: `${str(d.workflow) || "run"} ${id.slice(0, 8)} → ${str(d.status) || "?"}`,
    }];
  }
  if (event === "stage") {
    const id = str(d.id);
    if (!id) return [];
    return [{
      id: `stage:${id}:${str(d.finished_at) || now}`,
      ts: now, level: levelOf(d.status), source: "stage",
      message: `${str(d.stage_key) || "stage"} · run ${str(d.run_id).slice(0, 8)} → ${str(d.status) || "?"}`,
    }];
  }
  if (event === "session") {
    const id = str(d.id);
    if (!id) return [];
    const who = `${str(d.tool)}:${str(d.session_key).slice(0, 12)}`;
    const doing = str(d.doing) || str(d.ask);
    return [{
      id: `session:${id}:${str(d.created_at) || now}`,
      ts: now, level: levelOf(d.status), source: "session",
      message: `${who} → ${str(d.status) || "?"}${doing ? ` · ${doing.slice(0, 80)}` : ""}`,
    }];
  }
  return [];
}

function extLogToEntry(log: ExternalLog): DockEntry | null {
  const body = str(log.body).trim();
  if (!body) return null;
  const ts = log.timestamp ? new Date(log.timestamp).getTime() : Date.now();
  return {
    id: `ext:${log.id ?? `${ts}:${body.slice(0, 24)}`}`,
    ts: Number.isFinite(ts) ? ts : Date.now(),
    level: logLevel(log),
    source: "ext-log",
    message: body.length > 300 ? body.slice(0, 300) + "…" : body,
  };
}

export function EventDock() {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(COLLAPSED_KEY) === "1"; } catch { return false; }
  });
  const [filter, setFilter] = useState<Filter>("ALL");
  const [paused, setPaused] = useState(false);
  const [sseEntries, setSseEntries] = useState<DockEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      try { localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1"); } catch { /* noop */ }
      return !c;
    });
  };

  // SSE 이벤트 버스 구독 — 단일 EventSource 재사용 (두 번째 연결 없음)
  useSseEvents((e) => {
    if (e.event === "snapshot") return;
    const entries = sseToEntries(e.event, e.data);
    if (entries.length === 0) return;
    setSseEntries((prev) => [...prev, ...entries].slice(-MAX_ENTRIES));
  });

  // 외부 로그 — lead+ 전용. member는 쿼리 자체를 끄고, 403 수신 시 폴링 중지.
  const role = useAppStore((s) => s.role);
  const logsQ = useQuery({
    queryKey: ["event-dock", "external-logs"],
    queryFn: () => apiGet<ExternalLogsResponse>("/observability/external-logs?limit=50"),
    enabled: roleAllows("lead", role),
    refetchInterval: (q) =>
      q.state.error instanceof ApiError && q.state.error.status === 403 ? false : 30000,
    retry: false,
  });

  const entries = useMemo<DockEntry[]>(() => {
    const logEntries = (logsQ.data?.items || [])
      .map(extLogToEntry)
      .filter((x): x is DockEntry => x !== null);
    return [...sseEntries, ...logEntries]
      .sort((a, b) => a.ts - b.ts)
      .slice(-MAX_ENTRIES);
  }, [sseEntries, logsQ.data]);

  const visible = useMemo(
    () => (filter === "ALL" ? entries : entries.filter((e) => e.level === filter)),
    [entries, filter],
  );

  // 자동 스크롤 — paused 아니면 맨 아래 유지. 스크롤 업 시 onScroll이 pause.
  useEffect(() => {
    if (collapsed || paused) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [visible.length, collapsed, paused]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setPaused(!nearBottom);
  };

  const levelCls: Record<Level, string> = {
    INFO: "text-accent border-accent-border bg-accent-soft",
    WARN: "text-warn border-warn-border bg-warn-soft",
    ERROR: "text-err border-err-border bg-err-soft",
  };

  if (collapsed) {
    return (
      <div className="flex h-8 shrink-0 items-center gap-2 border-t border-border bg-surface px-3">
        <button
          type="button"
          onClick={toggleCollapsed}
          className="text-xs font-medium text-muted hover:text-ink"
          aria-label={t("dock.expand", "이벤트 도크 펼치기")}
        >
          ▴ {t("dock.title", "이벤트")}
        </button>
        <span className="font-mono text-[10px] text-muted">{visible.length}</span>
      </div>
    );
  }

  return (
    <section className="flex h-52 shrink-0 flex-col border-t border-border bg-surface" aria-label={t("dock.title", "이벤트 도크")}>
      <header className="flex h-8 shrink-0 items-center gap-2 border-b border-border px-2">
        <button type="button" onClick={toggleCollapsed} className="text-xs font-medium text-muted hover:text-ink" aria-label={t("dock.collapse", "도크 접기")}>
          ▾ {t("dock.title", "이벤트")}
        </button>
        <div className="flex items-center gap-1" role="group" aria-label={t("dock.filter", "레벨 필터")}>
          {(["ALL", "INFO", "WARN", "ERROR"] as Filter[]).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                filter === f ? "bg-accent-soft text-accent" : "text-muted hover:text-ink"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {paused && (
            <button type="button" onClick={() => setPaused(false)} className="text-[10px] font-medium text-warn hover:underline">
              {t("dock.paused", "일시정지됨 — 재개")}
            </button>
          )}
          <span className="font-mono text-[10px] text-muted">{visible.length}/{entries.length}</span>
        </div>
      </header>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto px-2 py-1 font-mono text-[11px] leading-5"
      >
        {visible.length === 0 ? (
          <div className="p-3 text-center text-muted">{t("dock.empty", "표시할 이벤트가 없습니다")}</div>
        ) : (
          visible.map((e) => (
            <div key={e.id} className="flex items-baseline gap-2 whitespace-nowrap">
              <span className="shrink-0 text-muted">{fmtTs(new Date(e.ts).toISOString())}</span>
              <span className={`shrink-0 rounded border px-1 text-[9px] font-bold leading-4 ${levelCls[e.level]}`}>{e.level}</span>
              <span className="shrink-0 text-disabled">{e.source}</span>
              <span className="truncate text-ink-2">{maskSecrets(e.message)}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
