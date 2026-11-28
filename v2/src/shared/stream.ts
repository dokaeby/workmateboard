/**
 * SSE 실시간 스트림 훅 — 레거시 index.html의 _sseConnect를 React Query로 이식.
 *
 * GET /api/v1/stream 은 첫 이벤트로 snapshot(전체 runs), 이후 run/stage/session
 * 데이터 이벤트를 보낸다. EventSource는 헤더를 못 쓰므로 API 키(localStorage
 * 'jobmate_api_key')가 있으면 POST /stream/token으로 단기 토큰을 받아
 * ?token= 으로 접속하고, 없으면 쿠키 세션으로 직접 접속한다.
 *
 * 이벤트 수신 시 관련 queryKey만 디바운스(~300ms) 무효화한다 — 폴링보다
 * 즉각 반영되면서 불필요한 refetch를 줄인다. 연결 오류 시 10초 후 토큰
 * 재발급 포함 재연결. document.hidden이어도 연결·무효화는 유지한다
 * (백그라운드 탭 갱신 목적; 서버 heartbeat가 연결을 유지).
 */
import { useEffect, useRef, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPost } from "./api/client";

const RETRY_MS = 10000;
const DEBOUNCE_MS = 300;

/**
 * SSE 연결 상태 — Global Header 상태등(US2)이 구독한다.
 * "online"=스트림 수신 가능, "connecting"=토큰 발급/연결 중,
 * "reconnecting"=오류 후 재시도 대기, "off"=비활성(미인증 등).
 */
export type SseState = "off" | "connecting" | "online" | "reconnecting";

interface SseStatus {
  state: SseState;
  /** 마지막 데이터 이벤트 수신 시각(ms epoch) — 없으면 null. */
  lastEventAt: number | null;
}

let sseStatus: SseStatus = { state: "off", lastEventAt: null };
const sseListeners = new Set<() => void>();

function setSseStatus(next: Partial<SseStatus>) {
  sseStatus = { ...sseStatus, ...next };
  for (const fn of sseListeners) fn();
}

function subscribeSse(fn: () => void): () => void {
  sseListeners.add(fn);
  return () => sseListeners.delete(fn);
}

/** 헤더/상태등용 SSE 상태 구독 훅. */
export function useSseStatus(): SseStatus {
  return useSyncExternalStore(subscribeSse, () => sseStatus);
}

/**
 * SSE 데이터 이벤트 버스 — spec 013 US2 Bottom Event Dock용.
 * useSseInvalidation이 여는 단일 EventSource 연결을 재사용한다
 * (두 번째 연결을 만들지 않는다). 서버 이벤트: snapshot/run/stage/session.
 * data는 JSON 파싱 결과(파싱 실패 시 null) — 행의 "ts" 필드는 서버가 제거해
 * 보낸다. 구독자는 반드시 자기 렌더 경로에서만 사용하고, 해제는 자동.
 */
export interface SseEvent {
  event: "snapshot" | "run" | "stage" | "session" | string;
  data: unknown;
}

type SseEventHandler = (e: SseEvent) => void;
const sseEventListeners = new Set<SseEventHandler>();

export function subscribeSseEvents(fn: SseEventHandler): () => void {
  sseEventListeners.add(fn);
  return () => {
    sseEventListeners.delete(fn);
  };
}

function emitSseEvent(event: string, ev: Event) {
  let data: unknown = null;
  try {
    data = JSON.parse((ev as MessageEvent).data);
  } catch {
    data = null;
  }
  for (const fn of sseEventListeners) fn({ event, data });
}

/** SSE 이벤트 구독 훅 — handler는 ref로 유지해 매 렌더 재구독하지 않는다. */
export function useSseEvents(handler: SseEventHandler): void {
  const ref = useRef(handler);
  useEffect(() => {
    ref.current = handler;
  });
  useEffect(() => subscribeSseEvents((e) => ref.current(e)), []);
}

/** run/stage 이벤트가 건드리는 queryKey prefix 목록 (invalidateQueries는 prefix 매칭). */
const RUN_KEYS = [
  "runs", "run", "run-timeline", "runs-stats", "office-runs", "office-run",
  "approvals", "badge-count", "automation-overview",
  "inbox", "inbox-unread-count", "workflows", "calendar-events", "calendar-jobs",
];

/** session 이벤트가 건드리는 queryKey prefix 목록. */
const SESSION_KEYS = [
  "agent-sessions", "office-sessions", "office-ended-sessions",
  "session-history", "office-session-stats", "office-session-activity",
  "office-schedules", "badge-count", "inbox", "inbox-unread-count",
];

const ALL_KEYS = [...new Set([...RUN_KEYS, ...SESSION_KEYS])];

export function useSseInvalidation(enabled = true): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) {
      setSseStatus({ state: "off" });
      return;
    }
    let es: EventSource | null = null;
    let retryTimer: number | undefined;
    let kickTimer: number | undefined;
    let disposed = false;
    const pending = new Set<string>();

    const flush = () => {
      kickTimer = undefined;
      if (pending.size === 0) return;
      const keys = [...pending];
      pending.clear();
      for (const k of keys) {
        void queryClient.invalidateQueries({ queryKey: [k] });
      }
    };

    const schedule = (keys: string[]) => {
      for (const k of keys) pending.add(k);
      if (kickTimer !== undefined) window.clearTimeout(kickTimer);
      kickTimer = window.setTimeout(flush, DEBOUNCE_MS);
    };

    const markEvent = () => setSseStatus({ lastEventAt: Date.now() });

    const connect = async () => {
      if (disposed) return;
      setSseStatus({ state: "connecting" });
      try {
        let url = "/api/v1/stream";
        const apiKey = localStorage.getItem("jobmate_api_key") || "";
        if (apiKey) {
          try {
            const d = await apiPost<{ token: string }>("/stream/token");
            url += "?token=" + encodeURIComponent(d.token);
          } catch {
            // 토큰 발급 실패 — 쿠키 세션으로 접속 시도 (실패하면 onerror 재시도)
          }
        }
        if (disposed) return;
        es?.close();
        es = new EventSource(url);
        es.onopen = () => setSseStatus({ state: "online" });
        es.addEventListener("run", (ev) => { markEvent(); schedule(RUN_KEYS); emitSseEvent("run", ev); });
        es.addEventListener("stage", (ev) => { markEvent(); schedule(RUN_KEYS); emitSseEvent("stage", ev); });
        es.addEventListener("session", (ev) => { markEvent(); schedule(SESSION_KEYS); emitSseEvent("session", ev); });
        // 연결 직후 전체 스냅샷 — 활성 쿼리 1회 무효화로 최신 상태 반영
        es.addEventListener("snapshot", (ev) => { markEvent(); schedule(ALL_KEYS); emitSseEvent("snapshot", ev); });
        es.onerror = () => {
          // 서버 종료/토큰 만료 — 자동 재연결 대신 닫고 10초 후 재발급 재접속
          es?.close();
          es = null;
          setSseStatus({ state: "reconnecting" });
          if (!disposed) {
            retryTimer = window.setTimeout(() => void connect(), RETRY_MS);
          }
        };
      } catch {
        // localStorage/EventSource 생성 등 동기 예외 — connecting 고착 방지
        es?.close();
        es = null;
        setSseStatus({ state: "reconnecting" });
        if (!disposed) {
          retryTimer = window.setTimeout(() => void connect(), RETRY_MS);
        }
      }
    };

    void connect();

    return () => {
      disposed = true;
      setSseStatus({ state: "off" });
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (kickTimer !== undefined) window.clearTimeout(kickTimer);
      es?.close();
      es = null;
    };
  }, [queryClient, enabled]);
}
