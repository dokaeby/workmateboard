/** Agent Office 3D 보드 — 레거시 index.html의 officeRender* 계열을 React로 이식.
 * 노드 상태색·엣지 live/done/fail/dim·진행률·attempt ×N 배지·ORCH 노드·
 * 승인 대기 펄스·이벤트 로그를 레거시와 동일한 규칙으로 재현한다.
 * 데이터는 props로 받는다(부모가 office 데이터를 연결).
 * 보조 모듈: ./types(데이터 타입) ./layout(노드 좌표) ./demo(가상 run)
 *   ./format(시간/집계 헬퍼) ./nodes(노드·엣지 모델+아이콘) ./svgs(보드 SVG)
 *   ./details(상세/로그 패널) ./i18n(사전) */
import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useI18n } from "../../../shared/i18n";
import { OFFICE_STAGES, OFFICE_NODES } from "./layout";
import { demoRunState } from "./demo";
import { fmtSecs, fmtElapsedRun, latestStageRuns, parseMs } from "./format";
import { nodeInfo, OFFICE_ICONS } from "./nodes";
import { GridSvg, FeedbackSvg, EdgesSvg } from "./svgs";
import { OrchDetail, DiffPreview, StageDetail, EventLog } from "./details";
import "./i18n";
import type { BoardSelection, StageBoardProps } from "./types";
import "./office-board.css";


// ── 메인 보드 ──

export function StageBoard({
  run: runProp,
  stageRuns: stageRunsProp,
  selected,
  onSelect,
  demo = false,
  onApprove,
  diff,
  diffLoading = false,
  className = "",
}: StageBoardProps) {
  const { t } = useI18n();
  // 1초 틱 — 실행 중 단계 소요시간/승인 대기 경과시간 갱신용
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // demo 모드: run이 없고 demo=true면 가상 run을 주기적으로 합성
  const [demoState, setDemoState] = useState(() => (demo && !runProp ? demoRunState() : null));
  useEffect(() => {
    if (!demo || runProp) return;
    const id = setInterval(() => setDemoState(demoRunState()), 1000);
    return () => clearInterval(id);
  }, [demo, runProp]);

  const run = runProp ?? demoState?.run ?? null;
  const stageRuns = stageRunsProp ?? demoState?.stageRuns ?? run?.stages;

  const stages = useMemo(() => latestStageRuns(stageRuns), [stageRuns]);

  // 선택 상태 — controlled(selected prop) 또는 내부 상태(기본 ORCH)
  const [innerSel, setInnerSel] = useState<BoardSelection>({ type: "orch" });
  const sel: BoardSelection | null =
    selected !== undefined ? selected : run ? innerSel : null;
  const handleSelect = (s: BoardSelection) => {
    if (selected === undefined) setInnerSel(s);
    onSelect?.(s);
  };

  // 헤더 칩 — 레거시 officeRenderHeader와 동일 계산
  const tokens = useMemo(
    () => (stageRuns || []).reduce((acc, s) => acc + (s.tokens_in || 0) + (s.tokens_out || 0), 0),
    [stageRuns],
  );
  const done = OFFICE_STAGES.filter(
    ({ key }) => stages.get(key)?.status === "succeeded",
  ).length;
  const progress = Math.round((done / OFFICE_STAGES.length) * 100);

  // 승인 대기 펄스 — approval.created_at(없으면 requested_at/decided_at/updated_at) 기준
  const waitingSince = useMemo(() => {
    if (!run || run.status !== "waiting_approval") return null;
    const aps = run.approvals || [];
    const pend = aps.find((a) => !a.decided_at) || aps[aps.length - 1];
    const ts = pend && (pend.created_at || pend.requested_at || pend.decided_at)
      ? parseMs(pend.created_at || pend.requested_at || pend.decided_at)
      : parseMs(run.updated_at);
    return isNaN(ts) ? "idle" : ts;
  }, [run]);

  const [approveBusy, setApproveBusy] = useState(false);
  const handleApprove = async (decision: "approve" | "reject") => {
    if (!onApprove || approveBusy) return;
    setApproveBusy(true);
    try {
      await onApprove(decision);
    } finally {
      setApproveBusy(false);
    }
  };

  if (!run) {
    return (
      <div className={`office-board ${className}`}>
        <div className="office-empty">{t("office.emptyRuns", "실행된 dev_review 워크플로가 없습니다")}</div>
      </div>
    );
  }

  const isOrchSel = !sel || sel.type === "orch";
  const selNode = !isOrchSel
    ? OFFICE_NODES.find((n) => n.code === sel!.code) || null
    : null;
  const selStageRun = selNode?.key ? stages.get(selNode.key) || null : null;
  const showApprove = run.status === "waiting_approval" && !!onApprove;
  const showDiff = run.status === "waiting_approval" &&
    (isOrchSel || selNode?.code === "HUM" || selNode?.key === "approval");

  return (
    <div className={`office-board ${className}`}>
      {/* 상태 칩: 실행시간 / 총 토큰 / 진행률 */}
      <div className="office-status">
        <span className="office-chip">
          {t("office.chip.elapsed", "실행시간")} <b>{fmtElapsedRun(run)}</b>
        </span>
        <span className="office-chip">
          {t("office.chip.tokens", "총 토큰")} <b>{tokens.toLocaleString()}</b>
        </span>
        <span className="office-chip">
          {t("office.chip.progress", "진행률")} <b>{progress}%</b>
        </span>
        {demo && !runProp && (
          <span className="office-chip" style={{ borderColor: "#f59e0b", color: "#b45309" }}>
            {t("office.demoBadge", "DEMO")}
          </span>
        )}
      </div>

      {/* 승인 대기 펄스 배지 */}
      {run.status === "waiting_approval" && (
        <div className="office-waiting">
          <span className="office-waiting-dot" />
          <span>{t("office.waiting", "승인 대기")}</span>
          <b>
            {waitingSince === "idle"
              ? t("office.waitingIdle", "대기 중")
              : `${t("office.waitingFor", "대기")} ${fmtSecs((now - (waitingSince as number)) / 1000)}`}
          </b>
        </div>
      )}

      {/* 3D 보드 */}
      <div className="office-stage">
        <div className="office-perspective">
          <div className="office-plate">
            <GridSvg />
            <i className="office-block office-block-bl" />
            <i className="office-block office-block-br" />
            <FeedbackSvg />
            <EdgesSvg stages={stages} />
            {OFFICE_NODES.map((n) => {
              const info = nodeInfo(n.code, run, stages);
              const isSel = isOrchSel
                ? n.code === "ORCH"
                : n.code === sel!.code;
              const stCls = n.code === "ORCH" ? ` office-st-${info.statusText}` : "";
              // 실패 단계는 툴팁에 오류 요약
              let tip = t(`office.node.${n.code}`, n.label);
              if (n.key) {
                const fsr = stages.get(n.key);
                if (fsr && fsr.status === "failed" && fsr.error) {
                  tip += " — " + String(fsr.error).slice(0, 160);
                }
              }
              return (
                <div
                  key={n.code}
                  role="button"
                  tabIndex={0}
                  className={
                    `office-node office-node-${info.cls}` +
                    `${n.code === "ORCH" ? " office-node-orch" : ""}` +
                    `${isSel ? " office-node-selected" : ""}`
                  }
                  data-node={n.code}
                  title={tip}
                  style={{ left: `${n.x}%`, top: `${n.y}%`, "--nw": `${n.w}%` } as CSSProperties}
                  onClick={() => handleSelect(n.code === "ORCH" ? { type: "orch" } : { type: "stage", code: n.code })}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      handleSelect(n.code === "ORCH" ? { type: "orch" } : { type: "stage", code: n.code });
                    }
                  }}
                >
                  <i className="office-node-shadow" />
                  <i className="office-node-base" />
                  <div className="office-node-face">
                    <span className="office-node-ico">{OFFICE_ICONS[n.code]}</span>
                    <div className="office-node-body">
                      <div className="office-node-code">{n.code}</div>
                      <div className="office-node-label">{t(`office.node.${n.code}`, n.label)}</div>
                      <div className={`office-node-status${stCls}`}>{info.statusText}</div>
                    </div>
                  </div>
                  {info.retry > 1 && <span className="office-node-retry">×{info.retry}</span>}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* 상세 + 이벤트 로그 */}
      <div className="office-split">
        <div className="rounded-xl border border-border bg-surface p-4 shadow-sm">
          {!run ? (
            <div className="office-empty">{t("office.selectAgent", "선택 에이전트")}</div>
          ) : isOrchSel ? (
            <>
              <OrchDetail run={run} t={t} />
              {showDiff && (diffLoading
                ? <div className="text-xs text-muted" style={{ marginTop: 12 }}>{t("office.diffLoading", "diff 로딩…")}</div>
                : diff ? <DiffPreview diff={diff} t={t} /> : null)}
              {showApprove && (
                <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("approve")}
                    className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                  >
                    {t("office.approve", "승인")}
                  </button>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("reject")}
                    className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-surface-hover disabled:opacity-50"
                  >
                    {t("office.reject", "거절")}
                  </button>
                </div>
              )}
            </>
          ) : selNode ? (
            <>
              <StageDetail node={selNode} sr={selStageRun} t={t} />
              {showDiff && diff && <DiffPreview diff={diff} t={t} />}
              {showApprove && selNode.code === "HUM" && (
                <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("approve")}
                    className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
                  >
                    {t("office.approve", "승인")}
                  </button>
                  <button
                    type="button"
                    disabled={approveBusy}
                    onClick={() => handleApprove("reject")}
                    className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-surface-hover disabled:opacity-50"
                  >
                    {t("office.reject", "거절")}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="office-empty">{t("office.selectAgent", "선택 에이전트")}</div>
          )}
        </div>
        <EventLog audit={run.audit || []} t={t} />
      </div>
    </div>
  );
}
