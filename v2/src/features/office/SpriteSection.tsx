/** Agent Office — 스프라이트 사무실 섹션 (레거시 #jmo-wrap — 2D 캔버스 + 사람들 패널 + 활동 로그).
 * index.tsx에서 verbatim 추출. OfficePage가 <OfficeSpriteSection paused={...} />로 사용. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { AgentSession } from "../../shared/api/types";
import { useI18n } from "../../shared/i18n";
import SpriteBoard from "./SpriteBoard";
import { OfficeSidePanel } from "./OfficeSidePanel";
import { useOfficeDemo } from "./demoState";
import { modeOf, sessionsToPeople } from "./sprites/bridge";
import { demoPeople } from "./sprites/demo-people";
import * as I18N from "./sprites/office-i18n";
import type { LifeEvent } from "./sprites/types";
import type { SessionView } from "./types";


// ── 스프라이트 사무실 (레거시 #jmo-wrap — 2D 캔버스 + 사람들 패널 + 활동 로그) ──
// paused: 보드 모드일 때 시뮬레이션만 멈추고 마운트는 유지 — 토글해도 상태가 보존된다(레거시와 동일)

// 세션 필터 상태 — 레거시 localStorage 키 그대로 (officeTool/officeEnded/officeProject)
const OFFICE_TOOLS = ["codex", "claude", "gemini", "devin", "all"] as const;
type OfficeTool = (typeof OFFICE_TOOLS)[number];

function lsGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function lsSet(key: string, v: string) {
  try { localStorage.setItem(key, v); } catch { /* noop */ }
}

/** 세션 고정 핀 — 레거시 jobmate.officePins (JSON 배열, "tool:session_key") */
function useOfficePins(): [Set<string>, (id: string) => void] {
  const [pins, setPins] = useState<Set<string>>(() => {
    try {
      const p = JSON.parse(localStorage.getItem("jobmate.officePins") || "[]");
      return new Set(Array.isArray(p) ? p : []);
    } catch { return new Set(); }
  });
  const togglePin = useCallback((id: string) => {
    setPins((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      try { localStorage.setItem("jobmate.officePins", JSON.stringify([...next])); } catch { /* noop */ }
      return next;
    });
  }, []);
  return [pins, togglePin];
}

export function OfficeSpriteSection({ paused = false }: { paused?: boolean }) {
  const { lang, t } = useI18n();
  const [selId, setSelId] = useState<string | null>(null);
  const [lifeEvents, setLifeEvents] = useState<LifeEvent[]>([]);
  const [imgFailed, setImgFailed] = useState(false);
  // 데모 캐스트 전이용 틱 — 레거시 office-view.js provider 폴링(POLL_MS=3s)에 해당
  const [demoTick, setDemoTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setDemoTick((n) => n + 1), 3000);
    return () => clearInterval(id);
  }, []);

  // 세션 필터 — 레거시 office-tool-filter / office-ended-btn / project 셀렉트 / 검색
  const [tool, setTool] = useState<OfficeTool>(() => {
    const v = lsGet("jobmate.officeTool");
    return (OFFICE_TOOLS as readonly string[]).includes(v || "") ? (v as OfficeTool) : "all";
  });
  const [ended, setEnded] = useState(() => lsGet("jobmate.officeEnded") === "1" ? "1" : "0");
  const [project, setProject] = useState(() => lsGet("jobmate.officeProject") || "");
  const [query, setQuery] = useState("");
  const [demo] = useOfficeDemo();
  const [pins, togglePin] = useOfficePins();

  const pickTool = (v: OfficeTool) => { setTool(v); lsSet("jobmate.officeTool", v); };
  const pickEnded = (v: string) => { setEnded(v); lsSet("jobmate.officeEnded", v); };
  const pickProject = (v: string) => { setProject(v); lsSet("jobmate.officeProject", v); };

  // include_ended=true — 활성/종료 필터는 클라이언트에서 적용.
  // queryKey는 "all" — "active"(include_ended=false)와 캐시를 공유하면 안 된다.
  const { data, isLoading, error } = useQuery({
    queryKey: ["office-sessions", "all"],
    queryFn: () => apiGet<SessionView[]>("/agent-sessions?limit=50&include_ended=true"),
    refetchInterval: 10000,
  });

  const endedMode = ended === "1";
  // 레거시 jmSessionProvider와 동일한 필터 순서: tool → ended → project → query
  const baseList = useMemo(
    () => (data || []).filter((s) =>
      (tool === "all" || s.tool === tool) &&
      (endedMode ? s.status === "ended" : s.status !== "ended")),
    [data, tool, endedMode],
  );

  // 프로젝트 셀렉트 옵션 — tool+ended 필터까지 적용된 목록의 distinct project (레거시 _syncOfficeProjects)
  const projectOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of baseList) {
      if (s.project) set.add(String(s.project));
    }
    return [...set].sort();
  }, [baseList]);

  const people = useMemo(() => {
    let list = baseList;
    if (project) list = list.filter((s) => String(s.project || "") === project);
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter((s) =>
        [s.tool, s.project, s.session_key, s.title, s.doing, s.ask]
          .some((v) => String(v || "").toLowerCase().includes(q)));
    }
    const real = sessionsToPeople(list as AgentSession[], { includeEnded: endedMode })
      .map((p) => ({ ...p, pinned: pins.has(p.id) }));
    // 레거시 jmSessionProvider: 핀 고정 세션을 먼저(안정 정렬) + officeDemo ON이면 가상 캐스트를 뒤에
    real.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned));
    return demo ? [...real, ...demoPeople(demoTick)] : real;
  }, [baseList, project, query, endedMode, pins, demo, demoTick]);

  const onEvents = useCallback((evs: LifeEvent[]) => {
    setLifeEvents((prev) => [...prev, ...evs].slice(-300));
  }, []);

  // 패널 클릭 — keep=true 면 강제 선택(로그 줄), 아니면 같은 id 재클릭 시 해제(레거시 select 와 동일)
  const pick = useCallback((id: string | null, keep?: boolean) => {
    setSelId((prev) => (id && (keep || id !== prev) ? id : null));
  }, []);

  const summary = useMemo(() => {
    const c = (m: string) => people.filter((p) => modeOf(p) === m).length;
    const w = c("working"), d = c("decision"), r = c("rest");
    const subs = people.reduce((n, p) => n + (p.subCount || 0), 0);
    return I18N.t("sum", { w, d, r }) + (subs ? I18N.t("sum.subs", { n: subs }) : "");
    // lang 은 useI18n 구독용 — 언어가 바뀌면 I18N.t 가 새 언어로 다시 만든다
  }, [people, lang]);

  // 레거시 bar(): 그림 로드 실패 → 연결 실패 → 로딩 순으로 안내 줄을 채운다
  const notice = [
    imgFailed ? I18N.t("note.img") : "",
    error ? I18N.t("note.net") : (!data && isLoading ? I18N.t("note.loading") : ""),
  ].filter(Boolean).join(" · ");

  // 세그먼트 버튼 공통 스타일 — 레거시 .office-mode-btn[aria-pressed] 대응
  const segBtn = (on: boolean) =>
    `rounded-md px-3 py-1 text-xs transition-colors ${
      on ? "bg-accent font-semibold text-white" : "text-muted hover:text-ink"
    }`;
  const segWrap = "flex items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5";
  const pinCount = people.filter((p) => p.pinned).length;

  return (
    <section className="jmo-office rounded-xl border border-border bg-surface p-3 shadow-sm">
      <div className="jmo-notice mb-2 flex flex-wrap items-baseline gap-x-3">
        <span>{summary}</span>
        {demo && <span className="jmo-demo">{I18N.t("demo")}</span>}
        {notice && <span className="text-warn">{notice}</span>}
      </div>
      {/* 세션 필터 바 — 레거시 #office-tool-filter + #office-session-filters */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className={segWrap} role="group" aria-label={t("office.toolFilter", "세션 도구 필터")}>
          {OFFICE_TOOLS.map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={tool === v}
              onClick={() => pickTool(v)}
              className={segBtn(tool === v)}
            >
              {v === "all" ? t("common.all", "전체") : v[0].toUpperCase() + v.slice(1)}
            </button>
          ))}
        </div>
        <div className={segWrap} role="group" aria-label={t("office.sessFilter", "세션 필터")}>
          <button type="button" aria-pressed={!endedMode} onClick={() => pickEnded("0")} className={segBtn(!endedMode)}>
            {t("office.active", "활성")}
          </button>
          <button type="button" aria-pressed={endedMode} onClick={() => pickEnded("1")} className={segBtn(endedMode)}>
            {t("office.ended", "종료")}
          </button>
          <select
            aria-label={t("office.projFilter", "프로젝트 필터")}
            value={project}
            onChange={(e) => pickProject(e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink-2"
          >
            <option value="">{t("office.allProjects", "전체 프로젝트")}</option>
            {projectOptions.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("office.sessSearch", "세션 검색…")}
            aria-label={t("office.sessSearch", "세션 검색…")}
            autoComplete="off"
            className="w-36 rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-accent"
          />
          {pinCount > 0 && (
            <span className="rounded-md border border-border px-2 py-1 text-xs text-muted">📌 {pinCount}</span>
          )}
        </div>
      </div>
      <div className="jmo-main">
        <div className="jmo-stage">
          <SpriteBoard
            className="jmo-box"
            people={people}
            selectedId={selId}
            onSelect={setSelId}
            onEvents={onEvents}
            onReady={(info) => setImgFailed(info.failedImage)}
            paused={paused}
          />
        </div>
        <OfficeSidePanel
          people={people}
          selectedId={selId}
          onPick={pick}
          events={lifeEvents}
          onTogglePin={togglePin}
        />
      </div>
    </section>
  );
}
