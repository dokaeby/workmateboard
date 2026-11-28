/**
 * Command Palette — spec 013 US3 (가이드 §8.10).
 * Cmd/Ctrl+K(핫키는 App.tsx) 또는 헤더 검색 버튼으로 오픈.
 * 검색 대상: nav 항목(역할/prefs 게이트), 최근 run, agent sessions,
 * inbox. 결과는 유형별 그룹, ↑↓/Enter/ESC 키보드 내비.
 * run/session 선택 → setInspector, nav/inbox → setView.
 * 최근 항목 localStorage "jm_palette_recent" 최대 10개.
 * 데이터는 팔레트가 열려 있을 때만 lazy fetch — Dialog는 open일 때만
 * 마운트되므로 열릴 때마다 상태가 자연스럽게 리셋된다.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../shared/api/client";
import type { AgentSession, InboxItem, RunListItem, ViewKey } from "../../shared/api/types";
import { useAppStore } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { NAV_ITEMS, visibleNavItems } from "../nav";

const RECENT_KEY = "jm_palette_recent";
const RECENT_MAX = 10;

type Group = "recent" | "nav" | "run" | "session" | "inbox";

interface PaletteItem {
  key: string;
  group: Group;
  icon: string;
  label: string;
  meta?: string;
  haystack: string;   // 검색 대상 소문자 텍스트
  select: () => void;
}

interface RecentEntry {
  group: Group;
  icon: string;
  label: string;
  meta?: string;
  view?: ViewKey;
  inspKind?: "run" | "agent" | "session" | "trace";
  inspId?: string;
}

function loadRecents(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(arr)) return [];
    // localStorage 변조 대비 화이트리스트 검증 — 유효 뷰는 nav 정의 기준
    const VALID_VIEWS = new Set<string>(NAV_ITEMS.map((i) => i.view));
    VALID_VIEWS.add("inbox");
    const VALID_KINDS = new Set(["run","agent","session","trace"]);
    return arr
      .filter((r): r is RecentEntry =>
        !!r && typeof r === "object" &&
        (typeof r.view === "string" ? VALID_VIEWS.has(r.view) : true) &&
        (r.inspKind == null || VALID_KINDS.has(r.inspKind)) &&
        (typeof r.label === "string" || typeof r.inspId === "string" || typeof r.view === "string"))
      .slice(0, RECENT_MAX);
  } catch {
    return [];
  }
}

function pushRecent(r: RecentEntry) {
  try {
    const dedupeKey = r.inspId ? `i:${r.inspKind}:${r.inspId}` : `v:${r.view}`;
    const next = [r, ...loadRecents().filter((x) => (x.inspId ? `i:${x.inspKind}:${x.inspId}` : `v:${x.view}`) !== dedupeKey)]
      .slice(0, RECENT_MAX);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch { /* noop */ }
}

const GROUP_LABEL: Record<Group, [string, string]> = {
  recent: ["palette.recent", "최근"],
  nav: ["palette.group.nav", "이동"],
  run: ["palette.group.runs", "실행"],
  session: ["palette.group.sessions", "세션"],
  inbox: ["palette.group.inbox", "알림"],
};

export function CommandPalette() {
  const open = useAppStore((s) => s.paletteOpen);
  if (!open) return null;
  return <PaletteDialog />;
}

function PaletteDialog() {
  const setOpen = useAppStore((s) => s.setPaletteOpen);
  const setView = useAppStore((s) => s.setView);
  const setInspector = useAppStore((s) => s.setInspector);
  const role = useAppStore((s) => s.role);
  const prefs = useAppStore((s) => s.prefs);
  const { t } = useI18n();

  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [recents] = useState<RecentEntry[]>(loadRecents);
  const listRef = useRef<HTMLDivElement>(null);

  // lazy fetch — Dialog는 open일 때만 마운트되므로 enabled 불필요
  const runsQ = useQuery({
    queryKey: ["palette", "runs"],
    queryFn: () => apiGet<RunListItem[]>("/runs?limit=20"),
    staleTime: 30000,
    retry: false,
  });
  const sessionsQ = useQuery({
    queryKey: ["palette", "sessions"],
    queryFn: () => apiGet<AgentSession[]>("/agent-sessions?limit=50&include_ended=false"),
    staleTime: 30000,
    retry: false,
  });
  const inboxQ = useQuery({
    queryKey: ["palette", "inbox"],
    queryFn: () => apiGet<InboxItem[]>("/inbox?limit=20"),
    staleTime: 30000,
    retry: false,
  });

  const groups = useMemo<{ group: Group; items: PaletteItem[] }[]>(() => {
    const query = q.trim().toLowerCase();
    const match = (hay: string) => !query || hay.toLowerCase().includes(query);
    const out: { group: Group; items: PaletteItem[] }[] = [];

    // 최근 항목 — 쿼리 없을 때만 (타이핑 중에는 현재 결과 우선)
    if (!query) {
      const items: PaletteItem[] = recents.map((r) => ({
        key: `recent:${r.inspId ?? r.view}`,
        group: "recent" as const,
        icon: r.icon,
        label: r.label,
        meta: r.meta,
        haystack: `${r.label} ${r.meta ?? ""}`,
        select: () => {
          if (r.inspKind && r.inspId) setInspector({ kind: r.inspKind, id: r.inspId });
          else if (r.view) setView(r.view);
          setOpen(false);
        },
      }));
      if (items.length) out.push({ group: "recent", items });
    }

    const navItems: PaletteItem[] = visibleNavItems(role, prefs)
      .filter((n) => match(`${n.label} ${n.view} ${t(`nav.${n.view}`, n.label)}`))
      .map((n) => ({
        key: `nav:${n.view}`,
        group: "nav" as const,
        icon: n.icon,
        label: t(`nav.${n.view}`, n.label),
        meta: n.view,
        haystack: `${n.label} ${n.view} ${t(`nav.${n.view}`, n.label)}`,
        select: () => {
          pushRecent({ group: "nav", icon: n.icon, label: t(`nav.${n.view}`, n.label), meta: n.view, view: n.view });
          setView(n.view);
          setOpen(false);
        },
      }));
    if (navItems.length) out.push({ group: "nav", items: navItems });

    const runItems: PaletteItem[] = (runsQ.data || [])
      .filter((r) => match(`${r.id} ${r.workflow ?? ""} ${r.status ?? ""} ${r.project ?? ""}`))
      .map((r) => ({
        key: `run:${r.id}`,
        group: "run" as const,
        icon: "▶",
        label: r.workflow || r.id.slice(0, 12),
        meta: `${r.id.slice(0, 8)} · ${r.status}`,
        haystack: `${r.id} ${r.workflow ?? ""} ${r.status ?? ""} ${r.project ?? ""}`,
        select: () => {
          pushRecent({ group: "run", icon: "▶", label: r.workflow || r.id.slice(0, 12), meta: `${r.id.slice(0, 8)} · ${r.status}`, inspKind: "run", inspId: r.id });
          setInspector({ kind: "run", id: r.id });
          setOpen(false);
        },
      }));
    if (runItems.length) out.push({ group: "run", items: runItems });

    const sessItems: PaletteItem[] = (sessionsQ.data || [])
      .filter((s) => match(`${s.session_key ?? ""} ${s.tool ?? ""} ${s.project ?? ""} ${s.doing ?? ""} ${s.title ?? ""}`))
      .map((s) => {
        const key = s.session_key || "";
        const label = s.title || s.doing || key.slice(0, 16);
        return {
          key: `session:${key}`,
          group: "session" as const,
          icon: "💬",
          label,
          meta: `${s.tool ?? ""} · ${s.status ?? ""}`,
          haystack: `${key} ${s.tool ?? ""} ${s.project ?? ""} ${s.doing ?? ""} ${s.title ?? ""}`,
          select: () => {
            pushRecent({ group: "session", icon: "💬", label, meta: `${s.tool ?? ""} · ${s.status ?? ""}`, inspKind: "session", inspId: key });
            setInspector({ kind: "session", id: key });
            setOpen(false);
          },
        };
      });
    if (sessItems.length) out.push({ group: "session", items: sessItems });

    const inboxItems: PaletteItem[] = (inboxQ.data || [])
      .filter((i) => match(`${i.title ?? ""} ${i.body ?? ""} ${i.kind ?? ""}`))
      .map((i, idx) => ({
        key: `inbox:${i.id ?? idx}`,
        group: "inbox" as const,
        icon: "🔔",
        label: i.title || i.kind || `#${i.id ?? idx}`,
        meta: i.kind,
        haystack: `${i.title ?? ""} ${i.body ?? ""} ${i.kind ?? ""}`,
        select: () => {
          pushRecent({ group: "inbox", icon: "🔔", label: i.title || i.kind || `#${i.id ?? idx}`, meta: i.kind, view: "inbox" });
          setView("inbox");
          setOpen(false);
        },
      }));
    if (inboxItems.length) out.push({ group: "inbox", items: inboxItems });

    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- t는 언어 변경 시에만 의미가 바뀐다
  }, [q, recents, role, prefs, runsQ.data, sessionsQ.data, inboxQ.data, setView, setInspector, setOpen]);

  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  // 결과 수가 줄어 active가 범위를 벗어나면 렌더 중 클램프 (setState-in-effect 회피)
  const activeIdx = flat.length === 0 ? 0 : Math.min(active, flat.length - 1);

  // active 항목 스크롤IntoView
  useEffect(() => {
    listRef.current
      ?.querySelector(`[data-idx="${activeIdx}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIdx]);

  const choose = (item: PaletteItem | undefined) => {
    if (item) item.select();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(flat.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(flat[activeIdx]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  };

  let idx = -1;
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-[12vh]"
      onClick={() => setOpen(false)}
      role="presentation"
    >
      <div
        className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-elevated shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-label={t("palette.title", "명령 팔레트")}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <span className="text-muted">⌘K</span>
          <input
            autoFocus
            value={q}
            onChange={(e) => { setQ(e.target.value); setActive(0); }}
            placeholder={t("palette.placeholder", "검색: 뷰 / 실행 / 세션 / 알림")}
            className="h-11 w-full bg-transparent text-sm text-ink outline-none placeholder:text-muted"
            aria-label={t("palette.placeholder", "검색")}
          />
          <kbd className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted">ESC</kbd>
        </div>
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto p-1.5">
          {flat.length === 0 ? (
            <div className="p-4 text-center text-sm text-muted">{t("palette.empty", "결과가 없습니다")}</div>
          ) : (
            groups.map((g) => (
              <div key={g.group}>
                <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
                  {t(GROUP_LABEL[g.group][0], GROUP_LABEL[g.group][1])}
                </div>
                {g.items.map((item) => {
                  idx += 1;
                  const myIdx = idx;
                  const isActive = myIdx === activeIdx;
                  return (
                    <button
                      key={item.key}
                      type="button"
                      data-idx={myIdx}
                      onMouseEnter={() => setActive(myIdx)}
                      onClick={() => choose(item)}
                      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm ${
                        isActive ? "bg-accent-soft text-ink" : "text-ink-2 hover:bg-surface-hover"
                      }`}
                    >
                      <span className="w-5 text-center">{item.icon}</span>
                      <span className="min-w-0 flex-1 truncate">{item.label}</span>
                      {item.meta ? <span className="shrink-0 truncate font-mono text-[10px] text-muted">{item.meta}</span> : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
