/** OfficeSidePanel — 레거시 office-panel.js 이식.
 * 오른쪽 패널: "사무실 사람들"(프로젝트별 그룹·펼침 상세) + 살아 움직이는 활동 로그.
 * 사람 목록은 people prop 을 정렬·묶어 그리고, 로그는 서버 활동 기록(d.log)과
 * 화면 사건(LifeEvent)을 시간순으로 섞어 450ms 마다 한 줄씩 흘려 넣는다(drip). */

import { useEffect, useMemo, useRef, useState } from "react";
import { colorOf, setSoundMuted, soundMuted } from "./sprites/office-life";
import { modeOf } from "./sprites/bridge";
import * as I18N from "./sprites/office-i18n";
import type { LifeEvent, LogEntry, PersonInput, SubInput } from "./sprites/types";
import { useI18n } from "../../shared/i18n";

const LOG_MAX = 60;
const DRIP_MS = 450;

const STATUS_CLS: Record<string, string> = { decision: "ask", working: "work", rest: "rest" };

interface LogRow {
  uid: string;
  at: number;          // ms epoch — 정렬·표시용
  id: string;
  name: string;
  color: string;
  act?: { k: string; a: string };
  mine?: boolean;
  key?: string;
  arg?: string;
}

const uidOf = (d: PersonInput, e: LogEntry) => `${d.id}|${e.at}|${e.k}|${e.a}`;
const textOf = (e: LogRow) => (e.act ? I18N.act(e.act) : I18N.ev(e.key || "", e.arg || ""));

export interface OfficeSidePanelProps {
  people: PersonInput[];
  selectedId?: string | null;
  onPick?: (id: string | null, keep?: boolean) => void;
  /** SpriteBoard 가 흘려보낸 화면 사건 배치들 */
  events?: LifeEvent[];
}

export function OfficeSidePanel({ people, selectedId, onPick, events }: OfficeSidePanelProps) {
  const { t, lang } = useI18n();
  const [items, setItems] = useState<LogRow[]>([]);
  const queueRef = useRef<LogRow[]>([]);
  const seenRef = useRef<Set<string>>(new Set());
  const seenEventsRef = useRef<Set<string>>(new Set());
  const primedRef = useRef(false);
  const [muted, setMuted] = useState(() => soundMuted());
  const peopleRef = useRef<HTMLUListElement>(null);
  const flashRef = useRef<string | null>(null);

  // ---------- 활동 로그 — 서버 기록 + 화면 사건을 같은 줄에 섞는다 ----------
  useEffect(() => {
    // 1) 서버 활동 기록(d.log) — uid 로 중복 제거, 첫 로드는 조용히, 이후는 큐에
    for (const d of people) {
      for (const e of d.log || []) {
        const ms = Date.parse(String(e.at || ""));
        if (!ms) continue;
        const uid = uidOf(d, e);
        if (seenRef.current.has(uid)) continue;
        seenRef.current.add(uid);
        const row: LogRow = {
          uid, at: ms, id: d.id, name: d.name, color: colorOf(d.name),
          act: { k: String(e.k || ""), a: String(e.a ?? "") },
        };
        if (primedRef.current) queueRef.current.push(row);
        else setItems((prev) => insertRow(prev, row));
      }
    }
    // 서버가 지금 주는 uid 집합으로 seen 을 갱신 — 오래 켜 둬도 안 불어난다
    const now = new Set<string>();
    for (const d of people) for (const e of d.log || []) now.add(uidOf(d, e));
    seenRef.current = now;
    // 2) 화면 사건(출근·퇴근·승인 대기 등) — 한 번만
    for (const e of events || []) {
      const uid = `ev|${e.at}|${e.id}|${e.key}|${e.arg}`;
      if (seenEventsRef.current.has(uid)) continue;
      seenEventsRef.current.add(uid);
      queueRef.current.push({
        uid, at: e.at, id: e.id, name: e.name, color: e.color,
        mine: true, key: e.key, arg: e.arg,
      });
    }
    queueRef.current.sort((a, b) => a.at - b.at);
    primedRef.current = true;
  }, [people, events]);

  // 큐에서 한 줄씩(450ms) — 최신이 위로 오도록 at 내림차순 삽입
  useEffect(() => {
    const timer = setInterval(() => {
      const e = queueRef.current.shift();
      if (e) setItems((prev) => insertRow(prev, e));
    }, DRIP_MS);
    return () => clearInterval(timer);
  }, []);

  function insertRow(prev: LogRow[], e: LogRow): LogRow[] {
    // 시간순 자리에 끼운다(맨 위가 최신) — 서버 기록은 조금 늦게 와도 제 시각 자리로
    const k = prev.findIndex((x) => x.at <= e.at);
    const at = k < 0 ? prev.length : k;
    const next = [...prev];
    next.splice(at, 0, e);
    return next.length > LOG_MAX ? next.slice(0, LOG_MAX) : next;
  }

  // ---------- 사람 목록 ----------
  const what = (d: PersonInput, open: boolean): string => {
    const mode = modeOf(d);
    if (mode === "decision") return I18N.cut(I18N.ask(d.ask) || I18N.t("needs"), open ? 80 : 40);
    if (mode === "working") return I18N.act(d.doing) || (d.subCount ? I18N.t("doing.subs") : I18N.t("doing.work"));
    return open ? I18N.t("d.idle") : "";
  };
  const subLine = (s: SubInput) =>
    `🐣 ${[s.type, s.desc].filter(Boolean).join(" · ") || I18N.t("d.subs")}`;

  const sorted = useMemo(() => {
    const order: Record<string, number> = { decision: 0, working: 1 };
    return [...people].sort((a, b) =>
      (order[modeOf(a)] ?? 2) - (order[modeOf(b)] ?? 2) || a.name.localeCompare(b.name));
  }, [people]);

  const groups = useMemo(() => {
    const map = new Map<string, PersonInput[]>();
    for (const p of sorted) {
      const k = p.project || "";
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(p);
    }
    const list = [...map.entries()].map(([proj, members]) => ({
      proj, members, busy: members.some((m) => modeOf(m) !== "rest"),
    }));
    const other = I18N.t("group.other");
    list.sort((a, b) => (a.busy === b.busy ? 0 : a.busy ? -1 : 1)
      || (a.proj || other).localeCompare(b.proj || other));
    return list;
  }, [sorted, t]);

  const working = sorted.filter((p) => modeOf(p) === "working").length;
  const subs = people.reduce((n, p) => n + (p.subCount || 0), 0);

  // 고른 항목이 목록 안에서 보이게 + 한 번 깜빡임
  useEffect(() => {
    if (!selectedId) { flashRef.current = null; return; }
    const box = peopleRef.current;
    const li = box?.querySelector<HTMLElement>(`[data-id="${CSS.escape(selectedId)}"]`);
    if (li && box) {
      const b = box.getBoundingClientRect(), r = li.getBoundingClientRect();
      if (r.top < b.top) box.scrollTop -= b.top - r.top + 4;
      else if (r.bottom > b.bottom) box.scrollTop += Math.min(r.top - b.top - 4, r.bottom - b.bottom + 4);
    }
    if (flashRef.current !== selectedId) {
      flashRef.current = selectedId;
      li?.classList.remove("jmo-flash");
      void li?.offsetWidth;
      li?.classList.add("jmo-flash");
    }
  }, [selectedId, people]);

  const details = (d: PersonInput) => {
    const facts: string[] = [I18N.t("d.sessions", { n: d.sessions || 1 })];
    if (d.commits != null) facts.push(I18N.t("d.commits", { n: d.commits }));
    if ((d.pending || 0) > 1) facts.push(I18N.t("d.pending", { n: d.pending ?? 0 }));
    if (d.source === "transcript") facts.push(I18N.t("d.transcript"));
    const meta: string[] = [];
    if (d.machine) meta.push(`${I18N.t("d.machine")} ${d.machine}`);
    const tok = (Number(d.tokens_in) || 0) + (Number(d.tokens_out) || 0);
    if (tok > 0) meta.push(I18N.t("d.tokens", { n: tok.toLocaleString(lang === "ko" ? "ko-KR" : "en-US") }));
    const cost = Number(d.est_cost_usd);
    if (d.est_cost_usd != null && isFinite(cost)) {
      meta.push(I18N.t("d.cost", { v: cost < 0.01 ? I18N.t("d.cost.low") : "$" + cost.toFixed(4) }));
    }
    const subs = d.subs || [];
    return { facts, meta, subs };
  };

  const renderPerson = (d: PersonInput) => {
    const open = d.id === selectedId;
    const mode = modeOf(d);
    const cls = STATUS_CLS[mode];
    const det = open ? details(d) : null;
    const w = what(d, open);
    return (
      <li key={d.id} className={`jmo-person${open ? " open" : ""}`} data-id={d.id}>
        <button
          type="button"
          className="jmo-head"
          aria-expanded={open}
          onClick={() => onPick?.(d.id)}
        >
          <i className="jmo-dot" style={{ background: colorOf(d.name) }} />
          <b>{d.name}</b>
          <span className={`jmo-chip ${cls}`}>{I18N.t(`status.${mode}`)}</span>
          {!!d.subCount && <span className="jmo-chip sub">{I18N.t("chip.subs", { n: d.subCount })}</span>}
          {(d.health === "stale" || d.health === "long_idle") && (
            <span className={`jmo-chip health ${d.health}`}>{I18N.t(`health.${d.health}`)}</span>
          )}
        </button>
        {w && (
          <p className={`jmo-doing ${cls}`}>
            {open && mode !== "rest" ? `${I18N.t("d.now")} · ${w}` : w}
          </p>
        )}
        {open && det && (
          <div className="jmo-more">
            {d.path && (
              <p className="jmo-path"><span className="jmo-k">{I18N.t("d.path")}</span><code>{d.path}</code></p>
            )}
            <p className="jmo-facts">{det.facts.join(" · ")}</p>
            {det.meta.length > 0 && <p className="jmo-facts">{det.meta.join(" · ")}</p>}
            {(det.subs.length > 0 || d.subCount) ? (
              <>
                <p className="jmo-k jmo-subhead">{I18N.t("d.subs")} {d.subCount || det.subs.length}</p>
                <ul className="jmo-subs">
                  {det.subs.map((s, i) => (
                    <li key={s.id ?? i}>
                      <span className="jmo-subname">{subLine(s)}</span>
                      <span className="jmo-subdoing">{I18N.act(s.doing) || I18N.t("d.subIdle")}</span>
                    </li>
                  ))}
                  {(d.subCount || 0) - det.subs.length > 0 && (
                    <li className="jmo-submore">{I18N.t("d.more", { n: (d.subCount || 0) - det.subs.length })}</li>
                  )}
                </ul>
              </>
            ) : null}
          </div>
        )}
        {!open && (d.subs || []).slice(0, 3).map((s, i) => (
          <p key={s.id ?? i} className="jmo-sub">
            {subLine(s)}{s.doing ? " — " + I18N.act(s.doing) : ""}
          </p>
        ))}
      </li>
    );
  };

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    setSoundMuted(next);
  };

  return (
    <div className="jmo-side">
      <div className="jmo-side-head">
        <h2>
          {I18N.t("side.people")}
          <small> {I18N.t("count", { n: sorted.length, w: working })}
            {subs ? I18N.t("sum.subs", { n: subs }) : ""}
          </small>
        </h2>
        <button
          type="button"
          className="jmo-mute"
          aria-pressed={muted}
          title={muted ? I18N.t("sound.off") : I18N.t("sound.on")}
          aria-label={muted ? I18N.t("sound.off") : I18N.t("sound.on")}
          onClick={toggleMute}
        >
          {muted ? "🔇" : "🔊"}
        </button>
      </div>
      <p className="jmo-hint">{I18N.t("side.hint")}</p>
      <ul className="jmo-people" ref={peopleRef}>
        {groups.map((g) => (
          <li key={g.proj || "__other"} className="jmo-group-wrap">
            <div className="jmo-group">
              <span className="jmo-group-name">{g.proj || I18N.t("group.other")}</span>
              <span className="jmo-group-n">{g.members.length}</span>
            </div>
            <ul className="jmo-group-items">
              {g.members.map(renderPerson)}
            </ul>
          </li>
        ))}
        {!people.length && <li className="jmo-empty">{I18N.t("note.empty")}</li>}
      </ul>
      <h2 className="jmo-log-title">{I18N.t("side.log")}</h2>
      <ul className="jmo-log">
        {items.map((e) => (
          <li
            key={e.uid}
            className={`jmo-logrow${e.mine ? "" : " fresh"}`}
            onClick={() => onPick?.(e.id, true)}
          >
            <time>{I18N.time(e.at)}</time>
            <i className="jmo-dot" style={{ background: e.color }} />
            <b>{e.name}</b>
            <span className={e.mine ? "jmo-ev" : undefined}>{textOf(e)}</span>
          </li>
        ))}
        {!items.length && <li className="jmo-empty">{t("office.noActivity", "활동 기록이 없습니다")}</li>}
      </ul>
    </div>
  );
}

export default OfficeSidePanel;
