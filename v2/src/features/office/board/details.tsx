/** Agent Office 3D 보드 — 상세 패널 하위 컴포넌트.
 * StageBoard.tsx에서 verbatim 이동: Kv / StageDetail / OrchDetail /
 * diffCap200 / DiffPreview / EventLog.
 * i18n은 useI18n이 아니라 `t` prop으로 주입받는다(부모가 전달). */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { jsonSummary } from "../../../shared/lib/format";
import type { OfficeNodeDef } from "./layout";
import type {
  BoardAuditEvent, BoardRun, BoardStageRun, StageBoardProps,
} from "./types";
import { fmtDuration, fmtElapsedRun, fmtTsRaw } from "./format";

// ── 상세 패널 ──

export function Kv({ k, v, children }: { k: string; v?: ReactNode; children?: ReactNode }) {
  return (
    <div className="office-kv">
      <span>{k}</span>
      <b>{children ?? v ?? "-"}</b>
    </div>
  );
}

export function StageDetail({
  node, sr, t,
}: {
  node: OfficeNodeDef;
  sr: BoardStageRun | null;
  t: (k: string, f?: string) => string;
}) {
  const head = (
    <div className="text-base font-bold text-ink" style={{ margin: "0 0 4px", overflowWrap: "anywhere" }}>
      {node.code} · {t(`office.node.${node.code}`, node.label)}
    </div>
  );
  if (!sr) {
    return (
      <div>
        {head}
        <Kv k={t("office.kv.status", "상태")} v="pending" />
        <div className="text-xs text-muted">{t("office.notRun", "아직 실행되지 않은 단계입니다.")}</div>
      </div>
    );
  }
  const tokens = (sr.tokens_in || 0) + (sr.tokens_out || 0);
  // 실패 단계: 오류를 빨간 박스로 강조 + 출력 앞부분 함께 표시
  const failBox = sr.status === "failed" ? (
    <div className="office-err-box">
      <b>{t("office.kv.failCause", "실패 원인")}</b>
      {"\n"}{sr.error || t("office.kv.noError", "(오류 메시지 없음)")}
    </div>
  ) : null;
  const outSlice = sr.status === "failed" ? 800 : 1200;
  const now = Date.now();
  return (
    <div>
      {head}
      <Kv k={t("office.kv.stage", "Stage")} v={node.key || "-"} />
      <Kv k={t("office.kv.status", "상태")} v={sr.status} />
      <Kv k={t("office.kv.attempt", "시도")} v={`attempt ${sr.attempt || 1}`} />
      <Kv k={t("office.kv.model", "모델")} v={sr.model || "-"} />
      <Kv k={t("office.kv.tokens", "토큰")}
        v={`in ${sr.tokens_in || 0} / out ${sr.tokens_out || 0} (${t("office.kv.tokensTotal", "합계")} ${tokens})`} />
      <Kv k={t("office.kv.duration", "소요시간")} v={fmtDuration(sr, now)} />
      <Kv k={t("office.kv.started", "시작")} v={fmtTsRaw(sr.started_at)} />
      <Kv k={t("office.kv.finished", "완료")} v={fmtTsRaw(sr.finished_at)} />
      {failBox}
      {sr.error && sr.status !== "failed" && (
        <Kv k={t("office.kv.error", "오류")} v={sr.error} />
      )}
      <div className="office-kv" style={{ alignItems: "flex-start" }}>
        <span>{t("office.kv.input", "입력")}</span>
        <b>{jsonSummary(sr.input, 300)}</b>
      </div>
      <div className="office-kv"><span>{t("office.kv.output", "출력")}</span></div>
      <pre className="office-pre">
        {JSON.stringify(sr.output || {}, null, 2).slice(0, outSlice)}
      </pre>
    </div>
  );
}

export function OrchDetail({ run, t }: { run: BoardRun; t: (k: string, f?: string) => string }) {
  const approvals = run.approvals || [];
  return (
    <div>
      <div className="text-base font-bold text-ink" style={{ margin: "0 0 4px", overflowWrap: "anywhere" }}>
        {t("office.orchTitle", "ORCH · 오케스트레이터")}
      </div>
      <Kv k={t("office.kv.status", "상태")} v={run.status} />
      <Kv k={t("office.kv.workflow", "워크플로")}
        v={`${run.workflow || "-"} v${run.version ?? "-"}`} />
      <Kv k={t("office.kv.runId", "Run ID")} v={run.id} />
      <Kv k={t("office.kv.created", "생성")} v={fmtTsRaw(run.created_at)} />
      <Kv k={t("office.kv.updated", "갱신")} v={fmtTsRaw(run.updated_at)} />
      <Kv k={t("office.kv.elapsed", "실행시간")} v={fmtElapsedRun(run)} />
      {run.error ? <Kv k={t("office.kv.error", "오류")} v={run.error} /> : null}
      <div className="office-kv" style={{ alignItems: "flex-start" }}>
        <span>{t("office.kv.input", "입력")}</span>
        <b>{jsonSummary(run.input, 400)}</b>
      </div>
      {approvals.length
        ? approvals.map((a, i) => (
          <Kv key={i} k={t("office.kv.approvals", "승인")}
            v={`${a.decision} · ${a.stage_key} · ${a.decided_by} · ${fmtTsRaw(a.decided_at)}${a.comment ? " — " + a.comment : ""}`} />
        ))
        : <Kv k={t("office.kv.approvals", "승인")} v={t("office.kv.noApprovals", "기록 없음")} />}
    </div>
  );
}

export function diffCap200(diff: string | undefined, t: (k: string, f?: string) => string): string {
  const s = String(diff ?? "");
  const lines = s.split("\n");
  return lines.length > 200
    ? lines.slice(0, 200).join("\n") + "\n" + t("office.diffTruncated", "… (이하 생략)")
    : s;
}

export function DiffPreview({ diff, t }: { diff: StageBoardProps["diff"]; t: (k: string, f?: string) => string }) {
  const [open, setOpen] = useState<number | null>(null);
  const files = diff?.files || [];
  const summary = diff?.summary
    ? typeof diff.summary === "string"
      ? diff.summary
      : JSON.stringify(diff.summary).slice(0, 400)
    : "";
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--muted)", marginBottom: 4 }}>
        {t("office.diffTitle", "승인 대상 변경 사항 (diff)")}
      </div>
      {summary && <div className="text-xs text-muted" style={{ marginBottom: 6 }}>{summary}</div>}
      {!files.length && <div className="text-xs text-muted">{t("office.diffEmpty", "변경된 파일이 없습니다")}</div>}
      {files.map((f, i) => (
        <div key={i}>
          <button
            type="button"
            className="office-diff-head"
            onClick={() => setOpen(open === i ? null : i)}
          >
            <span style={{ fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>{f.path}</span>
            <span className="office-chip" style={{ padding: "1px 8px" }}>{f.status || "modified"}</span>
            <span className="text-xs text-muted" style={{ marginLeft: "auto", flex: "none" }}>
              +{f.additions || 0} / −{f.deletions || 0}
            </span>
          </button>
          {open === i && (
            <pre className="office-pre office-diff-pre">{diffCap200(f.diff, t)}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

// ── 이벤트 로그 (run.audit 시간순, 최신 하단) ──

export function EventLog({ audit, t }: { audit: BoardAuditEvent[]; t: (k: string, f?: string) => string }) {
  const ref = useRef<HTMLDivElement>(null);
  const prevLen = useRef(0);
  const grew = audit.length > prevLen.current;
  useEffect(() => {
    // 새 이벤트가 추가되면 하단으로 스크롤, 길이 감소(run 교체) 시 맨 위로
    const el = ref.current;
    if (el) el.scrollTop = grew ? el.scrollHeight : 0;
    prevLen.current = audit.length;
  }, [audit.length, grew]);

  return (
    <div
      ref={ref}
      className="max-h-72 overflow-y-auto rounded-xl border border-border bg-surface p-4 shadow-sm"
    >
      <h2 className="mb-2 text-base font-semibold text-ink">{t("office.eventLog", "이벤트 로그")}</h2>
      {!audit.length && <div className="office-empty">{t("office.noEvents", "이벤트 없음")}</div>}
      {audit.map((a, i) => {
        const det = a.detail && (typeof a.detail === "string"
          ? a.detail
          : Object.keys(a.detail).length
            ? JSON.stringify(a.detail).slice(0, 160)
            : "");
        return (
          <div className="office-log-row" key={a.id || i}>
            <span className="office-log-time">{fmtTsRaw(a.created_at)}</span>
            <span className="office-log-actor">{a.actor}</span>
            <span className="office-log-action">{a.action}</span>
            {a.object_ref ? <span className="office-log-ref">{a.object_ref}</span> : null}
            {det ? <span className="office-log-detail">{det}</span> : null}
          </div>
        );
      })}
    </div>
  );
}
