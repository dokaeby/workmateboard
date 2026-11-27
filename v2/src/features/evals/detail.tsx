/** evals — 평가 상세 카드(편집 전환 + 지금 실행 + 실행 이력). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import { Card, Badge, Button } from "../../shared/ui";
import { fmtTs, jsonSummary } from "../../shared/lib/format";
import {
  statusOf, checkLabel,
  type EvalItem, type EvalRunResponse,
} from "./types";
import { EvalForm } from "./form";
import { EvalRuns } from "./runs";

export function EvalDetail({ item, workflowKeys }: { item: EvalItem; workflowKeys: string[] }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState("");

  // 목록 아이템은 요약 필드만 올 수 있으므로 상세는 단건 조회로 보강한다.
  const detail = useQuery({
    queryKey: ["eval", item.id],
    queryFn: () => apiGet<EvalItem>(`/evals/${encodeURIComponent(item.id)}`),
  });
  const full = detail.data || item;

  const runNow = async () => {
    setRunning(true);
    setMsg("");
    try {
      await apiPost<EvalRunResponse>(`/evals/${encodeURIComponent(item.id)}/run`);
      setMsg(t("evals.runStarted", "평가 실행을 시작했습니다"));
      await qc.invalidateQueries({ queryKey: ["evals"] });
      await qc.invalidateQueries({ queryKey: ["eval-runs", item.id] });
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  if (editing) {
    return (
      <Card>
        <EvalForm
          initial={full}
          workflowKeys={workflowKeys}
          onDone={() => { setEditing(false); void detail.refetch(); }}
          onCancel={() => setEditing(false)}
        />
      </Card>
    );
  }

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {full.name || item.id}
          <Badge tone="accent">{full.workflow_key || "-"}</Badge>
          {statusOf(full) === "archived" && <Badge tone="muted">{t("evals.statusArchived", "보관됨")}</Badge>}
        </span>
      }
      actions={
        <div className="flex flex-wrap gap-1.5">
          <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => setEditing(true)}>
            {t("evals.edit", "편집")}
          </Button>
          <Button className="px-2.5 py-1 text-xs" disabled={running || statusOf(full) === "archived"} onClick={runNow}>
            {running ? t("evals.running", "실행 요청 중…") : `▶ ${t("evals.runNow", "지금 실행")}`}
          </Button>
        </div>
      }
    >
      <div className="mb-3 grid grid-cols-1 gap-1.5 text-xs text-muted sm:grid-cols-2">
        <div>{t("evals.inputSummary", "입력")}: {jsonSummary(full.input, 160)}</div>
        <div>
          {t("evals.createdAt", "등록일")} {fmtTs(full.created_at)} · {t("evals.updatedAt", "수정일")} {fmtTs(full.updated_at)}
        </div>
      </div>
      {!!full.checks?.length && (
        <div className="mb-3 flex flex-wrap gap-1">
          {full.checks.map((c, i) => (
            <Badge key={i} tone="muted">{checkLabel(t, c.type)}</Badge>
          ))}
        </div>
      )}
      {msg && <div className="mb-2 rounded-lg border border-accent/30 bg-accent-soft p-2 text-xs text-accent">{msg}</div>}
      <EvalRuns evalId={item.id} />
    </Card>
  );
}
