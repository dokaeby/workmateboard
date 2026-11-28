/** agents — 상세/편집 사이드 패널. */
import { useEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Badge, Button } from "../../shared/ui";
import { usd, usdFromMicro, fmtTs } from "../../shared/lib/format";
import type { Agent } from "./tree";
import { STATUS_TONE, statusLabel, statusOf, lastRunAgo } from "./shared";
import { AgentForm } from "./form";
import { AgentPermissionsSection } from "./permissions";

export function AgentDrawer({
  agent,
  agents,
  onClose,
  onChanged,
}: {
  agent: Agent;
  agents: Agent[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit" | "addReport">("view");
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const pressOutside = useRef(false);

  // 네이티브 <dialog> — showModal()이 inert/포커스/Esc/포커스 복귀를 처리.
  // 'close' 이벤트(Esc 등)에서 onClose로 React 상태를 동기화한다.
  useEffect(() => {
    const d = dialogRef.current;
    if (!d) return;
    if (!d.open) d.showModal();
    const sync = () => onClose();
    d.addEventListener("close", sync);
    return () => d.removeEventListener("close", sync);
  });

  // 프로그램적 닫기 — d.close()가 'close' 이벤트를 발생시켜 위 리스너가
  // onClose로 React 상태를 동기화한다(포커스도 invoker로 복귀).
  const closeDialog = () => {
    const d = dialogRef.current;
    if (d) d.close();
    else onClose();
  };

  // 라이트 디스미스 — pointerdown·click 모두 dialog 자신(=backdrop 표면)을
  // 타겟으로 하고 좌표가 패널 rect 밖일 때만 닫는다(선택 드래그·select 오인 방지).
  const outsideDialog = (e: { clientX: number; clientY: number; currentTarget: HTMLDialogElement }) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  const onBackdropPointerDown = (e: PointerEvent<HTMLDialogElement>) => {
    pressOutside.current = outsideDialog(e);
  };
  const onBackdropClick = (e: MouseEvent<HTMLDialogElement>) => {
    const dismiss = pressOutside.current && e.target === e.currentTarget && outsideDialog(e);
    pressOutside.current = false;
    if (dismiss) closeDialog();
  };

  const st = statusOf(agent);
  const byId = useMemo(() => new Map(agents.map((x) => [x.id, x])), [agents]);
  const parent = agent.reports_to ? byId.get(agent.reports_to) : undefined;
  const caps = Array.isArray(agent.capabilities) ? agent.capabilities : [];

  const archive = async () => {
    const msg = t("agents.confirmArchive", "'{name}' 에이전트를 보관 처리할까요? 조직도에서 숨겨지고 실행이 중단됩니다.")
      .replace("{name}", agent.name || agent.id);
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await apiDelete(`/agents/${encodeURIComponent(agent.id)}`);
      await qc.invalidateQueries({ queryKey: ["agents"] });
      onChanged();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      aria-label={agent.name || agent.id}
      onPointerDown={onBackdropPointerDown}
      onClick={onBackdropClick}
      className="start-auto end-0 top-0 bottom-0 m-0 h-dvh w-full max-w-md max-h-none overflow-y-auto border-l border-border bg-surface p-0 text-ink shadow-xl open:flex flex-col [&:modal]:overscroll-contain [&::backdrop]:overflow-hidden [&::backdrop]:overscroll-contain [&::backdrop]:bg-black/30"
    >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-base font-bold text-ink">
            {agent.name || agent.id}
          </h2>
          <button
            type="button"
            onClick={closeDialog}
            aria-label={t("agents.close", "닫기")}
            className="rounded-md px-2 py-1 text-muted hover:bg-surface-hover"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 p-4">
          {mode !== "view" ? (
            <AgentForm
              agents={agents}
              initial={mode === "edit" ? agent : null}
              presetReportsTo={mode === "addReport" ? agent.id : null}
              onDone={() => {
                setMode("view");
                onChanged();
              }}
            />
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="accent">{agent.adapter_type || "custom"}</Badge>
                <Badge tone={STATUS_TONE[st] ?? "muted"}>
                  {statusLabel(t, st)}
                </Badge>
                {agent.role && <Badge tone="muted">{agent.role}</Badge>}
              </div>

              {agent.description && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("agents.description", "설명")}</div>
                  <p className="text-sm text-ink-2">{agent.description}</p>
                </div>
              )}

              {caps.length > 0 && (
                <div>
                  <div className="mb-1 text-xs font-bold text-muted">{t("agents.capabilities", "기능")}</div>
                  <div className="flex flex-wrap gap-1">
                    {caps.map((c, i) => (
                      <Badge key={i} tone="accent">{c}</Badge>
                    ))}
                  </div>
                </div>
              )}

              <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted">{t("agents.monthlyBudget", "월 예산")}</dt>
                  <dd className="text-ink">
                    {agent.monthly_budget_microusd != null
                      ? usdFromMicro(agent.monthly_budget_microusd)
                      : agent.monthly_budget_usd != null
                        ? usd(agent.monthly_budget_usd)
                        : t("agents.noBudget", "제한 없음")}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.reportsTo", "보고 대상")}</dt>
                  <dd className="text-ink">{parent ? parent.name || parent.id : t("agents.noParent", "없음 (최상위)")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.runs30", "30일 실행")}</dt>
                  <dd className="text-ink">{agent.runs_30d ?? 0} · {usd(agent.cost_30d_usd ?? 0)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.lastRun", "최근 실행")}</dt>
                  <dd className="text-ink">{agent.last_run_at ? `${fmtTs(agent.last_run_at)} (${lastRunAgo(agent.last_run_at)})` : t("agents.neverRun", "실행 기록 없음")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.createdAt", "등록일")}</dt>
                  <dd className="text-ink">{fmtTs(agent.created_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">{t("agents.updatedAt", "수정일")}</dt>
                  <dd className="text-ink">{fmtTs(agent.updated_at)}</dd>
                </div>
              </dl>

              {/* 3차 spec §3차 D/F — per-agent capability 권한 규칙.
                  GET/PUT /management/agents/{id}/permissions (lead+ 게이트는 서버). */}
              <div className="border-t border-border pt-3">
                <AgentPermissionsSection agentId={agent.id} />
              </div>

              <div className="flex flex-wrap gap-2 border-t border-border pt-3">
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("edit")}>
                  {t("agents.edit", "편집")}
                </Button>
                <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={() => setMode("addReport")}>
                  {t("agents.addReport", "하위 에이전트 추가")}
                </Button>
                {st !== "archived" && (
                  <Button variant="danger" className="px-3 py-1.5 text-xs" disabled={busy} onClick={archive}>
                    {t("agents.archive", "보관")}
                  </Button>
                )}
              </div>
            </div>
          )}
        </div>
    </dialog>
  );
}
