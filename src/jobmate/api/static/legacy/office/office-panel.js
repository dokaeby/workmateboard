"use strict";
// 오른쪽 패널 — 사무실 사람들(누르면 펼쳐 자세히: 경로·세션·커밋·서브에이전트·지금 하는 일)
// + 살아 움직이는 활동 로그(한 줄씩 흘려 넣는다). 글은 전부 textContent 로만 넣는다.

const OfficePanel = (() => {
  const $ = id => document.getElementById(id);
  const t = (k, v) => JMO_I18N.t(k, v);
  const P = { people: [], sel: null, items: [], seen: new Set(), queue: [], primed: false, pick: null };
  const LOG_MAX = 60;

  // 화면의 상태 = 승인 대기 > 일(본체가 쉬어도 서브에이전트가 일하면 일하는 중) > 쉼 — office-life.js 와 같은 잣대
  const modeOf = d => (d.status === "decision" ? "decision" : d.status === "working" || d.subCount > 0 ? "working" : "rest");
  const STATUS = { decision: "ask", working: "work", rest: "rest" };
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function dot(color) { const d = el("i", "o-dot"); d.style.background = color; return d; }

  // ---------- 알림 소리 버튼 — "사무실 사람들" 머리글 오른쪽. 상태는 localStorage 에 남는다 ----------
  let muteBtn = null;
  function paintMute() {
    if (!muteBtn) return;
    const m = OfficeLife.soundMuted();
    muteBtn.textContent = m ? "🔇" : "🔊";
    muteBtn.setAttribute("aria-pressed", m ? "true" : "false");
    const label = t(m ? "sound.off" : "sound.on");
    muteBtn.title = label;
    muteBtn.setAttribute("aria-label", label);
  }
  function ensureMute() {
    if (muteBtn) { paintMute(); return; }
    const h2 = $("jmo-count")?.closest("h2");
    if (!h2) return;
    muteBtn = el("button", "o-mute");
    muteBtn.type = "button";
    muteBtn.onclick = () => { OfficeLife.setSoundMuted(!OfficeLife.soundMuted()); paintMute(); };
    h2.append(muteBtn);
    paintMute();
  }

  // ---------- 사람 목록 ----------
  function what(d, open) {
    const mode = modeOf(d);
    if (mode === "decision") return JMO_I18N.cut(JMO_I18N.ask(d.ask) || t("needs"), open ? 80 : 40);
    if (mode === "working") return JMO_I18N.act(d.doing) || (d.subCount ? t("doing.subs") : t("doing.work"));
    return open ? t("d.idle") : "";
  }
  function subLine(s) {
    const name = [s.type, s.desc].filter(Boolean).join(" · ") || t("d.subs");
    return `🐣 ${name}`;
  }
  function details(d) {
    const box = el("div", "o-more");
    if (d.path) {
      const p = el("p", "o-path");
      p.append(el("span", "o-k", t("d.path")), el("code", null, d.path));
      box.append(p);
    }
    const facts = [t("d.sessions", { n: d.sessions || 1 })];
    if (d.commits != null) facts.push(t("d.commits", { n: d.commits }));
    if (d.pending > 1) facts.push(t("d.pending", { n: d.pending }));
    if (d.source === "transcript") facts.push(t("d.transcript"));
    box.append(el("p", "o-facts", facts.join(" · ")));
    // 세션 메타 — 머신·토큰·예상 비용은 있을 때만 한 줄로
    const meta = [];
    if (d.machine) meta.push(`${t("d.machine")} ${d.machine}`);
    const tok = (Number(d.tokens_in) || 0) + (Number(d.tokens_out) || 0);
    if (tok > 0) meta.push(t("d.tokens", { n: tok.toLocaleString(JMO_I18N.lang === "ko" ? "ko-KR" : "en-US") }));
    const cost = Number(d.est_cost_usd);
    if (d.est_cost_usd != null && isFinite(cost)) meta.push(t("d.cost", { v: cost < 0.01 ? t("d.cost.low") : "$" + cost.toFixed(4) }));
    if (meta.length) box.append(el("p", "o-facts", meta.join(" · ")));
    const subs = d.subs || [];
    if (subs.length || d.subCount) {
      box.append(el("p", "o-k o-subhead", `${t("d.subs")} ${d.subCount || subs.length}`));
      const ul = el("ul", "o-subs");
      for (const s of subs) {
        const li = el("li", null);
        li.append(el("span", "o-subname", subLine(s)), el("span", "o-subdoing", JMO_I18N.act(s.doing) || t("d.subIdle")));
        ul.append(li);
      }
      const more = (d.subCount || 0) - subs.length;
      if (more > 0) ul.append(el("li", "o-submore", t("d.more", { n: more })));
      box.append(ul);
    }
    return box;
  }
  function item(d) {
    const open = d.id === P.sel, mode = modeOf(d), cls = STATUS[mode];
    const li = el("li", "o-person" + (open ? " open" : ""));
    li.dataset.id = d.id;
    const head = el("button", "o-head");
    head.type = "button";
    head.setAttribute("aria-expanded", open ? "true" : "false");
    head.append(dot(OfficeLife.colorOf(d.name)), el("b", null, d.name), el("span", `o-chip ${cls}`, t("status." + mode)));
    if (d.subCount) head.append(el("span", "o-chip sub", t("chip.subs", { n: d.subCount })));
    if (d.health === "stale" || d.health === "long_idle") head.append(el("span", `o-chip health ${d.health}`, t("health." + d.health)));
    head.onclick = () => P.pick?.(d.id);
    li.append(head);
    const w = what(d, open);
    if (w) li.append(el("p", `o-doing ${cls}`, open && mode !== "rest" ? `${t("d.now")} · ${w}` : w));
    if (open) li.append(details(d));
    else for (const s of (d.subs || []).slice(0, 3)) {
      li.append(el("p", "o-sub", `${subLine(s)}${s.doing ? " — " + JMO_I18N.act(s.doing) : ""}`));
    }
    return li;
  }
  // 고른 항목이 목록 안에서 보이게 — 목록 칸만 움직이고 페이지는 그대로 둔다(폰에서 캔버스가 안 밀려나게)
  function reveal(li) {
    const box = $("jmo-people"), b = box.getBoundingClientRect(), r = li.getBoundingClientRect();
    if (r.top < b.top) box.scrollTop -= b.top - r.top + 4;
    else if (r.bottom > b.bottom) box.scrollTop += Math.min(r.top - b.top - 4, r.bottom - b.bottom + 4);
  }
  function render(people, sel, o = {}) {
    ensureMute();
    P.people = people || [];
    P.sel = sel || null;
    const list = $("jmo-people");
    const order = { decision: 0, working: 1 };
    const sorted = [...P.people].sort((a, b) => (order[modeOf(a)] ?? 2) - (order[modeOf(b)] ?? 2) || a.name.localeCompare(b.name));
    $("jmo-count").textContent = t("count", { n: sorted.length, w: sorted.filter(p => modeOf(p) === "working").length });
    // 프로젝트별 묶기 — 일하는/결정 있는 그룹이 먼저, 나머지는 프로젝트 이름순. 그룹 안 순서는 위 정렬 그대로.
    const groups = new Map();
    for (const p of sorted) {
      const k = p.project || "";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(p);
    }
    const glist = [...groups.entries()].map(([proj, members]) => ({
      proj, members, busy: members.some(m => modeOf(m) !== "rest"),
    }));
    glist.sort((a, b) => (a.busy === b.busy ? 0 : a.busy ? -1 : 1)
      || (a.proj || t("group.other")).localeCompare(b.proj || t("group.other")));
    const nodes = [];
    for (const g of glist) {
      const head = el("li", "o-group");
      head.append(el("span", "o-group-name", g.proj || t("group.other")),
                  el("span", "o-group-n", String(g.members.length)));
      nodes.push(head, ...g.members.map(item));
    }
    const keep = list.scrollTop;
    list.replaceChildren(...nodes);
    list.scrollTop = keep;
    if (o.focus && P.sel) {
      const li = list.querySelector(`li[data-id="${CSS.escape(P.sel)}"]`);
      if (li) {
        reveal(li);
        li.classList.remove("flash");
        void li.offsetWidth;
        li.classList.add("flash");
      }
    }
  }

  // ---------- 활동 로그 ----------
  // 서버 기록(Act)과 화면이 만든 사건(출근·퇴근 등)을 같은 줄에 섞는다. 글은 그릴 때 지금 언어로 만든다.
  // uid = 중복 거르기용 열쇠(서버 기록 한 줄 = 사람 + 시각 + 활동). 사건의 key(문구 열쇠)와 다른 칸이다.
  const uidOf = (d, e) => `${d.id}|${e.at}|${e.k}|${e.a}`;
  const textOf = e => (e.act ? JMO_I18N.act(e.act) : JMO_I18N.ev(e.key, e.arg));
  function collect(people, events) {
    const now = new Set();
    for (const d of people || []) {
      for (const e of d.log || []) {
        const ms = Date.parse(e.at);
        if (!ms) continue;
        const uid = uidOf(d, e);
        now.add(uid);
        if (P.seen.has(uid) || P.items.some(x => x.uid === uid) || P.queue.some(x => x.uid === uid)) continue;
        const row = { uid, at: ms, id: d.id, name: d.name, color: OfficeLife.colorOf(d.name), act: { k: e.k, a: e.a } };
        if (P.primed) P.queue.push(row); else add(row, true);
      }
    }
    P.seen = now;                               // 지금 서버가 주는 것만 기억 — 오래 켜 둬도 안 불어난다
    for (const e of events || []) P.queue.push({ ...e, uid: `ev|${e.at}|${e.id}|${e.key}|${e.arg}`, mine: true });
    P.queue.sort((a, b) => a.at - b.at);
    P.primed = true;
  }
  function row(e, quiet) {
    const li = el("li", quiet ? "o-log" : "o-log fresh");
    li.append(el("time", null, JMO_I18N.time(e.at)), dot(e.color), el("b", null, e.name), el("span", e.mine ? "o-ev" : null, textOf(e)));
    li.dataset.at = e.at;
    li.dataset.id = e.id;
    li.onclick = () => P.pick?.(e.id, true);
    return li;
  }
  // 시간순 자리에 끼운다(맨 위가 최신) — 서버 기록은 조금 늦게 와도 제 시각 자리로
  function add(e, quiet) {
    const box = $("jmo-log");
    const k = P.items.findIndex(x => x.at <= e.at);
    const at = k < 0 ? P.items.length : k;
    P.items.splice(at, 0, e);
    box.insertBefore(row(e, quiet), box.children[at] || null);
    while (P.items.length > LOG_MAX) { P.items.pop(); box.lastChild?.remove(); }
  }
  function drip() {
    const e = P.queue.shift();
    if (e) add(e, false);
  }
  // 언어가 바뀌면 지금 있는 글을 전부 새 언어로
  function relabel() {
    paintMute();
    $("jmo-log").replaceChildren(...P.items.map(e => row(e, true)));
    render(P.people, P.sel);
  }

  return { render, collect, drip, relabel, modeOf, onPick: f => { P.pick = f; } };
})();
