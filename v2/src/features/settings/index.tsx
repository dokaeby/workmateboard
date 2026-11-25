/** 설정 — 일반/알림 규칙/위젯/사이드바 메뉴 저장 + 시스템 상태 + 사용자 관리 + 계정. */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiGet, apiPost, ApiError } from "../../shared/api/client";
import type { Role } from "../../shared/api/types";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n, registerI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { NAV_ITEMS } from "../../app/nav";
import {
  NOTIFY_TOOLS, NOTIFY_STATUSES,
  normalizeNotifyRules, normalizeWidgets, widgetLabel,
  type NotifyRuleDraft,
} from "./lib";
import type {
  MeUser, NotifyRule, SettingsResponse, SettingsSaveResponse,
  SidebarMenuItem, WidgetDraft,
} from "./types";
import { STR } from "./i18n";
import SystemHealth from "./components/SystemHealth";
import UsersAdmin from "./components/UsersAdmin";
import AccountSection from "./components/AccountSection";
import PushSection from "./components/PushSection";

registerI18n(STR);

const SIDEBAR_ITEMS: SidebarMenuItem[] = NAV_ITEMS
  .filter((n) => n.view !== "office" && n.view !== "radar" && n.view !== "settings")
  .map((n) => ({ view: n.view, label: n.label, minRole: n.minRole }));

const inputCls =
  "rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none";
const selectCls = `${inputCls} h-9 py-0`;

interface Draft {
  gmail: string;
  radar: string;
  budget: string;
  rules: NotifyRuleDraft[];
  widgets: WidgetDraft[];
  menu: Record<string, boolean>;
}

function buildDraft(s: SettingsResponse | null | undefined): Draft {
  return {
    gmail: String(s?.gmail_check_minutes ?? 30),
    radar: s?.radar_interval || "daily",
    budget: s?.monthly_budget_usd != null ? String(s.monthly_budget_usd) : "",
    rules: normalizeNotifyRules(s?.notify_rules as NotifyRule[] | undefined),
    widgets: normalizeWidgets(s),
    menu: Object.fromEntries(SIDEBAR_ITEMS.map((it) => [it.view, s?.sidebar_menu?.[it.view] === true])),
  };
}

function draftToPrefs(d: Draft): Record<string, unknown> {
  const mb = d.budget.trim();
  return {
    gmail_check_minutes: Number(d.gmail),
    radar_interval: d.radar,
    notify_rules: d.rules.map((r) => ({
      tool: r.tool || "",
      project: r.project || "",
      statuses: r.statuses,
    })),
    monthly_budget_usd: mb === "" ? null : Number(mb),
    dashboard_widgets: d.widgets.map((w, i) => ({ id: w.id, visible: w.visible !== false, order: i })),
    sidebar_menu: { ...d.menu },
  };
}

function diffPrefs(next: Record<string, unknown>, base: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(next)) {
    if (JSON.stringify(next[k]) !== JSON.stringify(base[k])) out[k] = next[k];
  }
  return out;
}

export default function SettingsPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const setPrefs = useAppStore((s) => s.setPrefs);
  const storeRole = useAppStore((s) => s.role);

  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: () => apiGet<SettingsResponse>("/settings"),
    staleTime: 60000,
  });
  const meQ = useQuery({
    queryKey: ["me"],
    queryFn: () => apiGet<MeUser | { user?: MeUser }>("/auth/me"),
    staleTime: 60000,
    retry: false,
  });

  // /auth/me 응답은 {user:{...}} — App 캐시 형태와 섞여도 둘 다 처리
  const meUser = useMemo<MeUser | null>(() => {
    const d = meQ.data as { user?: MeUser } | MeUser | null | undefined;
    if (!d) return null;
    if (typeof d === "object" && "user" in d) return (d as { user?: MeUser }).user ?? null;
    return d as MeUser;
  }, [meQ.data]);

  const role: Role | null = (meUser?.role as Role | undefined) ?? storeRole;
  const isAdmin = roleAllows("admin", role);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [base, setBase] = useState<Record<string, unknown> | null>(null);
  const initialized = useRef(false);

  useEffect(() => {
    if (initialized.current || !settingsQ.data) return;
    initialized.current = true;
    const d = buildDraft(settingsQ.data);
    setDraft(d);
    setBase(draftToPrefs(d));
  }, [settingsQ.data]);

  const dirtyPayload = useMemo(() => {
    if (!draft || !base) return {};
    return diffPrefs(draftToPrefs(draft), base);
  }, [draft, base]);
  const dirty = Object.keys(dirtyPayload).length > 0;

  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<{ msg: string; tone?: "ok" | "err" }>({ msg: "" });

  const patch = (fn: (d: Draft) => Draft) => setDraft((d) => (d ? fn(d) : d));

  const applySavedPrefs = (prefs: Record<string, unknown> | undefined, fallbackDraft: Draft) => {
    if (prefs) {
      setPrefs({ ...(useAppStore.getState().prefs || {}), ...prefs });
      const d = buildDraft(prefs as SettingsResponse);
      setDraft(d);
      setBase(draftToPrefs(d));
    } else {
      setBase(draftToPrefs(fallbackDraft));
    }
    setSaveStatus({ msg: t("settings.saved", "저장했습니다"), tone: "ok" });
  };

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setSaveStatus({ msg: t("settings.saving", "저장 중…") });
    const payload = dirtyPayload;
    const currentDraft = draft!;
    try {
      const res = await apiPost<SettingsSaveResponse>("/settings", payload);
      applySavedPrefs(res?.prefs, currentDraft);
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      // 신규 키(monthly_budget_usd/dashboard_widgets/sidebar_menu)가 권한 부족으로
      // 거부되면 기존 키만으로 재시도 — 레거시 index.html과 동일한 폴백
      const legacy: Record<string, unknown> = {};
      for (const k of ["gmail_check_minutes", "radar_interval", "notify_rules"]) {
        if (k in payload) legacy[k] = payload[k];
      }
      if (Object.keys(legacy).length && e instanceof ApiError &&
          (e.status === 403 || /403|권한|forbidden|permission/i.test(e.message))) {
        try {
          const res = await apiPost<SettingsSaveResponse>("/settings", legacy);
          applySavedPrefs(res?.prefs, currentDraft);
          setSaveStatus({ msg: t("settings.saved_partial", "저장했습니다 (일부 항목은 권한 없음)"), tone: "ok" });
          qc.invalidateQueries({ queryKey: ["settings"] });
        } catch (e2) {
          setSaveStatus({ msg: e2 instanceof Error ? e2.message : String(e2), tone: "err" });
        }
      } else {
        setSaveStatus({ msg: e instanceof Error ? e.message : String(e), tone: "err" });
      }
    } finally {
      setSaving(false);
    }
  };

  /* ── 시스템 상태 lazy — 카드가 뷰포트에 들어오면 첫 로드 ── */
  const systemRef = useRef<HTMLDivElement | null>(null);
  const [healthActive, setHealthActive] = useState(false);
  useEffect(() => {
    const el = systemRef.current;
    if (!el || healthActive) return;
    if (!("IntersectionObserver" in window)) { setHealthActive(true); return; }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) {
        io.disconnect();
        setHealthActive(true);
      }
    }, { rootMargin: "120px" });
    io.observe(el);
    return () => io.disconnect();
  }, [healthActive]);

  if (settingsQ.isLoading || !draft) {
    return (
      <div className="mx-auto max-w-3xl p-4">
        <PageHead title={t("settings.subtitle", "설정")} />
        {settingsQ.error
          ? <ErrorBox message={settingsQ.error instanceof Error ? settingsQ.error.message : String(settingsQ.error)}
              onRetry={() => settingsQ.refetch()} />
          : <Spinner />}
      </div>
    );
  }
  const d = draft;
  const setField = <K extends keyof Draft>(k: K, v: Draft[K]) =>
    patch((prev) => ({ ...prev, [k]: v }));

  return (
    <div className="mx-auto max-w-3xl p-4 pb-24">
      <PageHead title={t("settings.subtitle", "설정")} />

      {/* ══ 일반 ══ */}
      <Card
        title={t("settings.sec.general", "일반")}
        className="mb-4"
      >
        <p className="mb-3 text-sm text-muted">{t("settings.sec.general.desc", "메일·스킬 레이더 수집 주기와 월 비용 예산을 설정합니다.")}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1.5 block text-sm font-semibold text-ink" htmlFor="f-gmail">
              {t("settings.gmail", "메일 체크 간격")}
            </label>
            <select id="f-gmail" className={selectCls} value={d.gmail}
              onChange={(e) => setField("gmail", e.target.value)}>
              <option value="30">{t("settings.gmail.30", "30분")}</option>
              <option value="60">{t("settings.gmail.60", "1시간")}</option>
            </select>
            <p className="mt-1 text-xs text-muted">{t("settings.gmail.hint", "Gmail 수집 작업이 실행되는 주기입니다.")}</p>
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-semibold text-ink" htmlFor="f-radar">
              {t("settings.radar", "스킬 레이더 체크 간격")}
            </label>
            <select id="f-radar" className={selectCls} value={d.radar}
              onChange={(e) => setField("radar", e.target.value)}>
              <option value="daily">{t("settings.radar.daily", "매일")}</option>
              <option value="weekly">{t("settings.radar.weekly", "매주")}</option>
            </select>
            <p className="mt-1 text-xs text-muted">{t("settings.radar.hint", "스킬·도구 레이더 스캔 주기입니다.")}</p>
          </div>
          <div className="sm:col-span-2">
            <label className="mb-1.5 block text-sm font-semibold text-ink" htmlFor="f-budget">
              {t("settings.budget", "월 예산 (USD)")}
            </label>
            <input id="f-budget" type="number" min="0" step="0.01" placeholder="0.00"
              className={`${inputCls} w-56`} value={d.budget}
              onChange={(e) => setField("budget", e.target.value)} />
            <p className="mt-1 text-xs text-muted">{t("settings.budget.hint", "월간 LLM 비용이 이 값을 넘으면 알림이 표시됩니다. 비워두면 제한 없음.")}</p>
          </div>
        </div>
      </Card>

      {/* ══ 세션 알림 규칙 ══ */}
      <Card title={t("settings.sec.notify", "세션 알림 규칙")} className="mb-4">
        <p className="mb-3 text-sm text-muted">{t("settings.sec.notify.desc", "규칙이 하나라도 있으면 조건에 맞는 세션만 알림을 보냅니다. 규칙이 없으면 전체 알림.")}</p>
        {!d.rules.length && (
          <p className="py-2 text-sm text-muted">{t("settings.notify.empty", "등록된 규칙 없음")}</p>
        )}
        <div className="flex flex-col">
          {d.rules.map((r, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2.5 border-b border-border py-2.5 last:border-0">
              <select
                className={`${selectCls} w-28`}
                value={r.tool}
                aria-label={t("settings.notify.tool", "툴")}
                onChange={(e) => patch((prev) => ({
                  ...prev,
                  rules: prev.rules.map((x, j) => (j === i ? { ...x, tool: e.target.value } : x)),
                }))}
              >
                {NOTIFY_TOOLS.map((tv) => (
                  <option key={tv} value={tv}>
                    {tv ? tv[0].toUpperCase() + tv.slice(1) : t("settings.notify.tool.all", "전체")}
                  </option>
                ))}
              </select>
              <input
                type="text" value={r.project}
                placeholder={t("settings.notify.project", "프로젝트 (비워두면 전체)")}
                autoComplete="off"
                className={`${inputCls} min-w-36 flex-1`}
                onChange={(e) => patch((prev) => ({
                  ...prev,
                  rules: prev.rules.map((x, j) => (j === i ? { ...x, project: e.target.value } : x)),
                }))}
              />
              <span className="inline-flex flex-wrap gap-x-3 gap-y-1">
                {NOTIFY_STATUSES.map((s) => {
                  const key = `settings.notify.${s}`;
                  const fallback = s === "working" ? "작업" : s === "decision" ? "결정" : "휴식";
                  return (
                    <label key={s} className="inline-flex cursor-pointer items-center gap-1 text-xs text-muted">
                      <input
                        type="checkbox" className="accent-accent"
                        checked={r.statuses.includes(s)}
                        onChange={(e) => patch((prev) => ({
                          ...prev,
                          rules: prev.rules.map((x, j) => j === i
                            ? { ...x, statuses: e.target.checked ? [...x.statuses, s] : x.statuses.filter((v) => v !== s) }
                            : x),
                        }))}
                      />
                      {t(key, fallback)}
                    </label>
                  );
                })}
              </span>
              <Button variant="ghost" className="px-2 py-1 text-xs" title="✕"
                onClick={() => patch((prev) => ({ ...prev, rules: prev.rules.filter((_, j) => j !== i) }))}>
                ✕
              </Button>
            </div>
          ))}
        </div>
        <Button variant="secondary" className="mt-2 px-3 py-1 text-xs"
          onClick={() => patch((prev) => ({ ...prev, rules: [...prev.rules, { tool: "", project: "", statuses: [] }] }))}>
          + {t("settings.notify.add", "규칙 추가")}
        </Button>
      </Card>

      {/* ══ 대시보드 위젯 ══ */}
      <Card title={t("settings.sec.widgets", "대시보드 위젯")} className="mb-4">
        <p className="mb-3 text-sm text-muted">{t("settings.sec.widgets.desc", "알림 화면에 표시할 카드와 순서를 정합니다.")}</p>
        <div className="flex flex-col">
          {d.widgets.map((w, i) => (
            <div key={w.id} className="flex items-center gap-3 border-b border-border py-2 last:border-0">
              <label className="inline-flex cursor-pointer items-center">
                <input type="checkbox" className="h-4 w-4 accent-accent"
                  checked={w.visible !== false}
                  onChange={(e) => patch((prev) => ({
                    ...prev,
                    widgets: prev.widgets.map((x, j) => (j === i ? { ...x, visible: e.target.checked } : x)),
                  }))}
                />
              </label>
              <span className="min-w-0 flex-1 truncate text-sm text-ink">{widgetLabel(w.id, t)}</span>
              <span className="inline-flex gap-1">
                <Button variant="secondary" className="px-2 py-0.5 text-xs" disabled={i === 0}
                  onClick={() => patch((prev) => {
                    const ws = [...prev.widgets];
                    [ws[i - 1], ws[i]] = [ws[i], ws[i - 1]];
                    return { ...prev, widgets: ws };
                  })}>↑</Button>
                <Button variant="secondary" className="px-2 py-0.5 text-xs" disabled={i === d.widgets.length - 1}
                  onClick={() => patch((prev) => {
                    const ws = [...prev.widgets];
                    [ws[i], ws[i + 1]] = [ws[i + 1], ws[i]];
                    return { ...prev, widgets: ws };
                  })}>↓</Button>
              </span>
            </div>
          ))}
        </div>
      </Card>

      {/* ══ 사이드바 메뉴 ══ */}
      <Card title={t("settings.sec.sidebar", "사이드바 메뉴")} className="mb-4">
        <p className="mb-3 text-sm text-muted">{t("settings.sec.sidebar.desc", "메뉴 표시 여부를 설정합니다. Agent-Office·Skill Radar는 항상 표시되며, 역할 제한 메뉴는 해당 권한이 있어야 보입니다.")}</p>
        <div className="flex flex-col">
          {SIDEBAR_ITEMS.map((it) => {
            const on = d.menu[it.view] === true;
            return (
              <div key={it.view} className="flex items-center gap-3 border-b border-border py-2 last:border-0">
                <span className="min-w-0 flex-1 truncate text-sm text-ink">{it.label}</span>
                {it.minRole && <Badge>{it.minRole}+</Badge>}
                <span className="inline-flex overflow-hidden rounded-lg border border-border">
                  {([true, false] as const).map((v) => (
                    <label key={String(v)}
                      className={`cursor-pointer px-3 py-1 text-xs font-medium ${
                        on === v ? "bg-accent-soft text-accent" : "bg-surface text-muted hover:bg-surface-hover"
                      }`}>
                      <input type="radio" className="sr-only" name={`sm-${it.view}`} checked={on === v}
                        onChange={() => patch((prev) => ({ ...prev, menu: { ...prev.menu, [it.view]: v } }))} />
                      {v ? t("settings.show", "표시") : t("settings.hide", "숨김")}
                    </label>
                  ))}
                </span>
              </div>
            );
          })}
        </div>
      </Card>

      {/* ══ 푸시 알림 ══ */}
      <PushSection />

      {/* ══ 시스템 상태 + 사용자 관리 (admin) ══ */}
      <div ref={systemRef}>
        <Card title={t("settings.sec.system", "시스템 상태")} className="mb-4">
          <p className="mb-3 text-sm text-muted">{t("settings.sec.system.desc", "서버·브리지·스케줄러 상태를 확인합니다.")}</p>
          {isAdmin ? (
            <SystemHealth active={healthActive} />
          ) : (
            <Empty>{t("settings.sec.users.desc", "관리자만 볼 수 있습니다.")}</Empty>
          )}
        </Card>
        {isAdmin && <UsersAdmin meId={meUser?.id} />}
      </div>

      {/* ══ 계정 ══ */}
      <div className="mt-4">
        <AccountSection me={meUser} />
      </div>

      {/* ══ 위험 구역 ══ */}
      <div className="mt-4">
        <DeleteAccountZone />
      </div>

      {/* ══ 저장 바 ══ */}
      <div className="sticky bottom-0 z-10 -mx-4 mt-4 border-t border-border bg-bg/90 px-4 py-3 backdrop-blur">
        <div className="flex items-center justify-between gap-3">
          <span className={`text-sm ${saveStatus.tone === "ok" ? "text-ok" : saveStatus.tone === "err" ? "text-err" : "text-muted"}`}>
            {saveStatus.msg || (dirty ? t("settings.unsaved", "저장되지 않은 변경사항이 있습니다") : "")}
          </span>
          <Button disabled={!dirty || saving} onClick={save}>
            {saving ? t("settings.saving", "저장 중…") : t("settings.save", "변경사항 저장")}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** 위험 구역 — AccountSection과 분리된 독립 카드(레거시 settings.html 하단 카드와 동일 위치). */
function DeleteAccountZone() {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ msg: string; tone?: "ok" | "err" }>({ msg: "" });

  const open = () => {
    if (!value.trim()) {
      setStatus({ msg: t("settings.danger.need_input", "확인 정보를 입력하세요"), tone: "err" });
      return;
    }
    setConfirmOpen(true);
  };

  const doDelete = async () => {
    const v = value.trim();
    setBusy(true);
    setStatus({ msg: t("settings.danger.working", "삭제 중…") });
    try {
      try {
        await api("/auth/account", { method: "DELETE", json: { password: v } });
      } catch (e1) {
        if (e1 instanceof ApiError && (e1.code === "confirm_required" || /confirm/i.test(e1.message))) {
          await api("/auth/account", { method: "DELETE", json: { confirm_email: v } });
        } else {
          throw e1;
        }
      }
      setConfirmOpen(false);
      setStatus({ msg: t("settings.danger.done", "계정이 삭제되었습니다 — 로그인 화면으로 이동합니다"), tone: "ok" });
      try { await apiPost("/auth/logout"); } catch { /* 이미 폐기됨 */ }
      try { localStorage.removeItem("jobmate_api_key"); } catch { /* noop */ }
      setTimeout(() => { window.location.href = "/"; }, 1200);
    } catch (e) {
      setStatus({ msg: e instanceof Error ? e.message : String(e), tone: "err" });
      setBusy(false);
    }
  };

  return (
    <section className="rounded-xl border border-err-border bg-err-soft p-4">
      <h2 className="text-base font-semibold text-err">{t("settings.sec.danger", "위험 구역")}</h2>
      <p className="mb-3 text-sm text-muted">{t("settings.sec.danger.desc", "계정 삭제는 되돌릴 수 없습니다. 모든 데이터가 영구 삭제되며 로그아웃됩니다.")}</p>
      <div className="flex flex-wrap items-center gap-2.5">
        <input
          type="text" autoComplete="off" value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={t("settings.danger.input", "비밀번호 또는 가입 이메일")}
          className="w-72 max-w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-err focus:outline-none"
        />
        <Button variant="danger" className="px-3 py-1.5 text-xs" disabled={busy} onClick={open}>
          {t("settings.danger.btn", "계정 삭제")}
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted">{t("settings.danger.hint", "비밀번호 계정은 비밀번호를, Google 로그인 계정은 가입 이메일을 입력하세요.")}</p>
      {status.msg && (
        <span className={`mt-1 block text-xs ${status.tone === "ok" ? "text-ok" : "text-err"}`}>{status.msg}</span>
      )}
      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
          <div className="w-full max-w-md rounded-xl border border-border bg-surface p-6 shadow-lg">
            <p className="text-base font-bold text-err">{t("settings.danger.dialog.title", "정말 계정을 삭제하시겠습니까?")}</p>
            <p className="mb-4 mt-2 text-sm text-muted">{t("settings.danger.dialog.body", "계정과 모든 관련 데이터가 즉시 영구 삭제됩니다. 이 작업은 되돌릴 수 없습니다.")}</p>
            <div className="flex justify-end gap-2">
              <Button variant="secondary" className="px-3 py-1.5 text-xs" disabled={busy} onClick={() => setConfirmOpen(false)}>
                {t("settings.danger.cancel", "취소")}
              </Button>
              <Button variant="danger" className="px-3 py-1.5 text-xs" disabled={busy} onClick={doDelete}>
                {busy ? t("settings.danger.working", "삭제 중…") : t("settings.danger.confirm", "삭제")}
              </Button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
