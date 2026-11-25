/** 커넥터 — 외부 서비스 연동 목록/추가/토글/삭제/항목 조회 (/connectors*). */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiDelete, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";
import "./i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { fmtTs, safeUrl } from "../../shared/lib/format";


interface Connector {
  id: string;
  type?: string;
  name?: string;
  enabled?: boolean;
  has_token?: boolean;
  last_fetch_at?: string;
  last_error?: string;
  created_at?: string;
}
interface ConnectorsResponse { items?: Connector[] }
interface ConnItem { id?: string; title?: string; url?: string; state?: string; updated_at?: string; author?: string }
interface ConnItemsResponse { items?: ConnItem[]; cached?: boolean }

type FieldType = "text" | "password";
const CONN_TYPES: Record<string, { label: string; fields: [string, string, FieldType][] }> = {
  github: { label: "GitHub", fields: [
    ["token", "토큰", "password"], ["owner", "소유자 (owner)", "text"], ["repo", "저장소 (repo)", "text"],
  ]},
  jira: { label: "Jira", fields: [
    ["url", "Jira URL", "text"], ["email", "이메일", "text"], ["token", "API 토큰", "password"],
  ]},
  notion: { label: "Notion", fields: [["url", "Notion API URL", "text"], ["token", "통합 토큰", "password"]] },
  linear: { label: "Linear", fields: [
    ["url", "Linear API URL", "text"], ["token", "API 토큰", "password"], ["team", "팀 키", "text"],
  ]},
  webhook: { label: "Webhook", fields: [
    ["url", "Webhook URL", "text"], ["secret", "시크릿 (선택)", "password"],
  ]},
  json: { label: "JSON (generic)", fields: [
    ["url", "JSON API URL", "text"], ["json_path", "항목 JSON path (선택)", "text"],
  ]},
};

const inputCls = "min-w-36 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm";

function NewConnectorForm({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [type, setType] = useState("github");
  const [name, setName] = useState("");
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; err?: boolean }>({ text: "" });
  const fields = CONN_TYPES[type]?.fields || [];

  const submit = async () => {
    if (!name.trim()) {
      setMsg({ text: t("conn.needName", "이름을 입력하세요"), err: true });
      return;
    }
    const config: Record<string, string> = {};
    for (const [k] of fields) {
      const v = (cfg[k] || "").trim();
      if (v) config[k] = v;
    }
    setBusy(true);
    setMsg({ text: t("common.adding", "추가 중…") });
    try {
      await apiPost("/connectors", { type, name: name.trim(), config, enabled: true });
      setName(""); setCfg({});
      setMsg({ text: t("common.added", "추가했습니다") });
      qc.invalidateQueries({ queryKey: ["connectors"] });
      onDone();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : String(e), err: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-border bg-surface-hover p-3">
      <div className="flex flex-wrap gap-2">
        <select value={type} onChange={(e) => { setType(e.target.value); setCfg({}); }} className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm">
          {Object.entries(CONN_TYPES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
        <input
          value={name} onChange={(e) => setName(e.target.value)}
          placeholder={t("conn.namePh", "이름 (예: 회사 GitHub)")}
          className={inputCls}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {fields.map(([k, label, ty]) => (
          <input
            key={k} type={ty} value={cfg[k] || ""} autoComplete="off"
            placeholder={t(`conn.f.${type}.${k}`, label)}
            onChange={(e) => setCfg((c) => ({ ...c, [k]: e.target.value }))}
            className={inputCls}
          />
        ))}
      </div>
      <div className="flex items-center justify-end gap-2">
        {msg.text && <span className={`text-xs ${msg.err ? "text-err" : "text-ok"}`}>{msg.text}</span>}
        <Button variant="secondary" className="px-3 py-1 text-xs" onClick={onDone}>{t("common.cancel", "취소")}</Button>
        <Button className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>{t("common.add", "추가")}</Button>
      </div>
    </div>
  );
}

function ConnectorItems({ connId }: { connId: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["connector-items", connId],
    queryFn: () => apiGet<ConnItemsResponse>(`/connectors/${encodeURIComponent(connId)}/items?limit=50`),
    enabled: open,
    staleTime: 30000,
  });
  const items = q.data?.items || [];
  return (
    <div className="mt-2">
      <div className="flex items-center gap-2">
        <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setOpen((v) => !v)}>
          {open ? t("conn.hideItems", "항목 숨기기") : t("conn.showItems", "항목 보기")}
        </Button>
        {open && (
          <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={q.isFetching} onClick={() => q.refetch()}>
            {q.isFetching ? t("common.loading", "로딩…") : t("conn.refreshItems", "항목 새로고침")}
          </Button>
        )}
        {open && q.data?.cached && <span className="text-xs text-muted">{t("conn.cached", "(캐시)")}</span>}
      </div>
      {open && (
        <div className="mt-1 space-y-1 rounded-md bg-surface-hover p-2">
          {q.isLoading && <div className="text-xs text-muted">{t("common.loading", "로딩…")}</div>}
          {q.error && <div className="text-xs text-err">{q.error instanceof Error ? q.error.message : String(q.error)}</div>}
          {q.data && !items.length && <div className="text-xs text-muted">{t("conn.noItems", "가져온 항목이 없습니다")}</div>}
          {items.map((it, i) => (
            <div key={it.id || i} className="flex flex-wrap items-baseline gap-x-2 text-xs">
              {it.url ? (
                <a href={safeUrl(it.url)} target="_blank" rel="noopener noreferrer" className="font-semibold text-accent break-all">
                  {it.title || it.url}
                </a>
              ) : (
                <span className="font-semibold text-ink break-all">{it.title || t("conn.noTitle", "(제목 없음)")}</span>
              )}
              {it.state && <Badge>{it.state}</Badge>}
              <span className="text-muted">
                {fmtTs(it.updated_at)}{it.author ? ` · ${it.author}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ConnectorCard({ c }: { c: Connector }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const enabled = c.enabled !== false;
  const typeLabel = CONN_TYPES[c.type || ""]?.label || c.type || "-";

  const toggle = async () => {
    setBusy(true);
    try {
      await apiPost(`/connectors/${encodeURIComponent(c.id)}/toggle`, { enabled: !enabled });
      qc.invalidateQueries({ queryKey: ["connectors"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    if (!confirm(t("conn.confirmDelete", "'{name}' 커넥터를 삭제할까요?").replace("{name}", c.name || c.id))) return;
    try {
      await apiDelete(`/connectors/${encodeURIComponent(c.id)}`);
      qc.invalidateQueries({ queryKey: ["connectors"] });
    } catch (e) {
      alert(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-ink">{c.name || t("conn.unnamed", "(이름 없음)")}</span>
        <Badge tone="accent">{typeLabel}</Badge>
        <Badge tone={enabled ? "ok" : "err"}>{enabled ? t("conn.enabled", "사용") : t("conn.disabled", "중지")}</Badge>
        {c.has_token && <Badge>{t("conn.tokenSet", "토큰 설정됨")}</Badge>}
      </div>
      <div className="mt-1 text-xs text-muted">
        {t("conn.lastFetch", "마지막 수집")}: {fmtTs(c.last_fetch_at)}
      </div>
      {c.last_error && <div className="mt-1 text-xs text-err">{t("conn.lastError", "최근 오류")}: {c.last_error}</div>}
      <div className="mt-2 flex flex-wrap gap-1.5">
        <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={busy} onClick={toggle}>
          {enabled ? t("conn.disable", "중지") : t("conn.enable", "사용")}
        </Button>
        <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={del}>
          {t("common.delete", "삭제")}
        </Button>
      </div>
      <ConnectorItems connId={c.id} />
    </div>
  );
}

export default function ConnectorsPage() {
  const { t } = useI18n();
  const [showNew, setShowNew] = useState(false);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiGet<ConnectorsResponse>("/connectors"),
  });
  const items = data?.items || [];

  return (
    <div className="mx-auto max-w-3xl p-4">
      <PageHead title={t("conn.title", "커넥터")} desc={t("conn.desc", "외부 서비스 연동 — 토큰은 서버에 암호화되어 저장됩니다")}>
        <Button className="text-xs" onClick={() => setShowNew((v) => !v)}>
          {showNew ? t("common.close", "닫기") : "+ " + t("conn.add", "커넥터 추가")}
        </Button>
        <Button variant="secondary" onClick={() => refetch()} className="text-xs">{t("common.refresh", "새로고침")}</Button>
      </PageHead>
      {showNew && <NewConnectorForm onDone={() => setShowNew(false)} />}
      <Card>
        {isLoading && <Spinner />}
        {error && <ErrorBox message={error instanceof Error ? error.message : String(error)} onRetry={() => refetch()} />}
        {data && !items.length && <Empty>{t("conn.empty", "연결된 커넥터가 없습니다")}</Empty>}
        <div className="space-y-3">
          {items.map((c) => <ConnectorCard key={c.id} c={c} />)}
        </div>
      </Card>
    </div>
  );
}
