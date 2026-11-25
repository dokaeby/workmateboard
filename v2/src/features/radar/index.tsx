/** Skill Radar — 플랫폼/기간/날짜 선택, 트렌딩·토픽·랭킹·저장소 상세. 레거시 loadRadar 이식. */
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useAppStore, roleAllows } from "../../shared/store/appStore";
import { useI18n } from "../../shared/i18n";
import { PageHead, Card, Empty, Spinner, ErrorBox, Button, Badge } from "../../shared/ui";
import { safeUrl } from "../../shared/lib/format";

const PLATFORMS = ["claude", "chatgpt", "gemini"];
const TAXONOMIES = ["all", "agents", "mcp", "skills", "plugins", "devtools", "automation", "data", "other"];
const LANGS = ["ko", "en", "zh"] as const;
type Lang = (typeof LANGS)[number];
type Period = "daily" | "week" | "month";

interface RadarItem {
  rank?: number;
  repo?: string;
  repo_full_name?: string;
  url?: string;
  repo_url?: string;
  appearances?: number;
  description?: string;
  stars?: number;
  stars_today?: number | null;
  language?: string;
  license?: string;
  topics?: string[];
  taxonomy?: string;
  platform?: string;
  summary?: string[];
}
interface RadarRepoDetail extends RadarItem {
  readme_excerpt?: string;
  summaries?: Record<string, string[]>;
  related_topics?: string[];
  collected_date?: string;
  snapshot_platform?: string;
}
interface Topic { topic: string; count: number }
interface Related { topic: string; score: number }

function fmtStars(n: number | null | undefined): string {
  const v = n || 0;
  return v >= 1000 ? (v / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(v);
}
const LICENSE_SHORT: Record<string, string> = {
  "Apache-2.0": "Apache-2.0", "MIT": "MIT", "BSD-2-Clause": "BSD-2",
  "BSD-3-Clause": "BSD-3", "GPL-3.0": "GPL-3", "GPL-2.0": "GPL-2",
  "AGPL-3.0": "AGPL-3", "LGPL-3.0": "LGPL-3", "MPL-2.0": "MPL-2.0",
  "ISC": "ISC", "Unlicense": "Unlicense", "0BSD": "0BSD",
  "CC0-1.0": "CC0", "EPL-2.0": "EPL-2", "BSL-1.0": "BSL",
  "Elastic-2.0": "Elastic-2", "MS-PL": "MS-PL", "Zlib": "Zlib",
  "WTFPL": "WTFPL", "Python-2.0": "PSF", "Artistic-2.0": "Artistic",
};
function licenseShort(license: string | undefined): string {
  if (!license) return "";
  if (LICENSE_SHORT[license]) return LICENSE_SHORT[license];
  const g = license.match(/^GPL-(\d+\.0)/);
  if (g) return `GPL-${g[1]}`;
  const b = license.match(/^BSD-(\d)-Clause/);
  if (b) return `BSD-${b[1]}`;
  return license.length > 12 ? license.slice(0, 12) : license;
}

function qs(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

function devReviewTask(item: RadarItem): string {
  const name = item.repo_full_name || item.repo || "(unknown)";
  const url = item.repo_url || item.url || "";
  const meta: string[] = [];
  if (item.platform) meta.push(`플랫폼 ${item.platform}`);
  if (item.taxonomy) meta.push(`분류 ${item.taxonomy}`);
  if (item.language) meta.push(`언어 ${item.language}`);
  if (item.stars != null) meta.push(`스타 ${item.stars}`);
  if (item.stars_today != null && item.stars_today !== 0) meta.push(`오늘 스타 +${item.stars_today}`);
  if (item.rank != null) meta.push(`일간 순위 ${item.rank}위`);
  if ((item.topics || []).length) meta.push(`주제: ${(item.topics || []).join(", ")}`);
  if ((item.summary || []).length) meta.push(`요약: ${(item.summary || []).join(" / ")}`);
  const lines = ["Skill Radar에서 발견된 저장소를 분석하고 개선 작업을 검토해줘:", "", `- 저장소: ${name}`];
  if (url) lines.push(`- URL: ${url}`);
  if (meta.length) lines.push(`- 정보: ${meta.join(" · ")}`);
  if (item.description) lines.push(`- 설명: ${item.description}`);
  lines.push("", "트렌딩에 오른 이유와 코드 품질·개선 포인트를 파악해줘.");
  return lines.join("\n");
}

export default function RadarPage() {
  const { t } = useI18n();
  const setView = useAppStore((s) => s.setView);
  const role = useAppStore((s) => s.role);
  const canLead = roleAllows("lead", role);
  const qc = useQueryClient();

  const [platform, setPlatform] = useState("claude");
  const [period, setPeriod] = useState<Period>("daily");
  const [date, setDate] = useState("");
  const [lang, setLang] = useState<Lang>("ko");
  const [taxFilter, setTaxFilter] = useState("all");
  const [minStars, setMinStars] = useState(0);
  const [topic, setTopic] = useState<string | null>(null);
  const [topicPlatform, setTopicPlatform] = useState("all");
  const [repoName, setRepoName] = useState<string | null>(null);
  const [refreshMsg, setRefreshMsg] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [runMsg, setRunMsg] = useState<Record<string, string>>({});

  const topicsQ = useQuery({
    queryKey: ["radar-topics"],
    queryFn: () => apiGet<{ topics?: Topic[] }>("/radar/topics"),
    refetchInterval: 300000,
  });
  const allTopics = topicsQ.data?.topics || [];

  const datesQ = useQuery({
    queryKey: ["radar-dates", platform],
    queryFn: () => apiGet<{ dates?: string[] }>(`/radar/dates?platform=${platform}`),
    refetchInterval: 300000,
  });
  const dates = datesQ.data?.dates || [];
  useEffect(() => {
    if (dates.length && (!date || !dates.includes(date))) setDate(dates[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datesQ.data]);

  const inList = !topic && !repoName;
  const trendingQ = useQuery({
    queryKey: ["radar-trending", platform, date, lang],
    queryFn: () => {
      const p = new URLSearchParams({ platform, lang });
      if (date) p.set("date", date);
      return apiGet<{ items?: RadarItem[] }>(`/radar/trending?${p.toString()}`);
    },
    enabled: inList && period === "daily",
  });
  const rankingQ = useQuery({
    queryKey: ["radar-ranking", platform, period, lang],
    queryFn: () => apiGet<{ items?: RadarItem[] }>(
      `/radar/ranking?${qs({ period: period === "week" ? "week" : "month", platform, lang })}`),
    enabled: inList && period !== "daily",
  });
  const topicReposQ = useQuery({
    queryKey: ["radar-topic-repos", topic, topicPlatform, lang],
    queryFn: () => apiGet<{ items?: RadarItem[] }>(
      `/radar/topic-repos?${qs({ topic: topic || "", platform: topicPlatform, lang, limit: "10" })}`),
    enabled: !!topic && !repoName,
  });
  const relatedQ = useQuery({
    queryKey: ["radar-related-topics", topic],
    queryFn: () => apiGet<{ related?: Related[] }>(`/radar/related-topics?topic=${encodeURIComponent(topic || "")}`),
    enabled: !!topic,
  });
  const repoQ = useQuery({
    queryKey: ["radar-repo", repoName, lang],
    queryFn: () => apiGet<{ item?: RadarRepoDetail }>(
      `/radar/repo/${encodeURIComponent(repoName || "")}?lang=${lang}`),
    enabled: !!repoName,
  });

  const rawItems: RadarItem[] = useMemo(() => {
    if (repoName) return [];
    if (topic) return topicReposQ.data?.items || [];
    if (period === "daily") return trendingQ.data?.items || [];
    return rankingQ.data?.items || [];
  }, [repoName, topic, period, topicReposQ.data, trendingQ.data, rankingQ.data]);

  const items = useMemo(() => rawItems.filter((it) =>
    (taxFilter === "all" || it.taxonomy === taxFilter) && (it.stars || 0) >= minStars),
    [rawItems, taxFilter, minStars]);

  const listLoading = topic ? topicReposQ.isLoading
    : period === "daily" ? trendingQ.isLoading : rankingQ.isLoading;
  const listError = topic ? topicReposQ.error : period === "daily" ? trendingQ.error : rankingQ.error;

  const selectTopic = (tp: string) => {
    setTopic(tp);
    setTopicPlatform("all");
    setRepoName(null);
  };
  const clearTopic = () => { setTopic(null); setRepoName(null); };

  const refresh = async () => {
    setRefreshing(true);
    setRefreshMsg(t("radar.collecting", "수집 run 시작 중…"));
    try {
      const r = await apiPost<{ run_ids?: string[] }>("/radar/refresh", {
        platforms: PLATFORMS, force: true,
      });
      setRefreshMsg(t("radar.refreshDone", "run {ids} — 자동 승인 후 발행됩니다")
        .replace("{ids}", (r.run_ids || []).join(",")));
      qc.invalidateQueries({ queryKey: ["radar-topics"] });
    } catch (e) {
      setRefreshMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  };

  const submitDevReview = async (item: RadarItem, key: string) => {
    const name = item.repo_full_name || item.repo || item.repo_url || "";
    if (!name || runMsg[key] === "busy") return;
    setRunMsg((m) => ({ ...m, [key]: "busy" }));
    try {
      const task = devReviewTask(item);
      await apiPost("/runs", {
        workflow: "dev_review", task,
        input: { task },
        metadata: { source: "skill_radar", repo: name },
      });
      setRunMsg((m) => ({ ...m, [key]: "ok" }));
      qc.invalidateQueries({ queryKey: ["runs"] });
    } catch (e) {
      setRunMsg((m) => ({ ...m, [key]: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const topicCount = topic ? (allTopics.find((x) => x.topic === topic)?.count || 0) : 0;
  const related = (relatedQ.data?.related || []).filter((r) => r.topic !== topic).slice(0, 8);

  return (
    <div className="mx-auto max-w-6xl p-4">
      <PageHead
        title={t("radar.title", "Skill Radar")}
        desc={t("radar.desc", "AI skill · plugin trending — GitHub Search API · 일일 Top 10 · LLM 3줄 요약")}
      >
        <Button className="text-xs" disabled={refreshing || !canLead} title={canLead ? "" : t("radar.needLead", "lead 이상 권한 필요")}
          onClick={refresh}>
          {refreshing ? t("radar.collecting", "수집 중…") : t("radar.refresh", "수집")}
        </Button>
        <Button variant="secondary" className="text-xs" onClick={() => {
          qc.invalidateQueries({ queryKey: ["radar-topics"] });
          qc.invalidateQueries({ queryKey: ["radar-dates"] });
          qc.invalidateQueries({ queryKey: ["radar-trending"] });
          qc.invalidateQueries({ queryKey: ["radar-ranking"] });
          qc.invalidateQueries({ queryKey: ["radar-topic-repos"] });
        }}>
          {t("common.refresh", "새로고침")}
        </Button>
        <select value={lang} onChange={(e) => setLang(e.target.value as Lang)}
          className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink">
          {LANGS.map((l) => <option key={l} value={l}>{l === "ko" ? "한국어" : l === "en" ? "English" : "中文"}</option>)}
        </select>
      </PageHead>
      {refreshMsg && <div className="mb-3 text-xs text-muted">{refreshMsg}</div>}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex gap-1 rounded-lg border border-border bg-surface p-0.5">
          {PLATFORMS.map((p) => (
            <button key={p} onClick={() => setPlatform(p)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium capitalize transition-colors ${platform === p ? "bg-accent text-white" : "text-muted hover:bg-surface-hover"}`}>
              {p}
            </button>
          ))}
        </div>
        <div className="flex gap-1 rounded-lg border border-border bg-surface p-0.5">
          {(["daily", "week", "month"] as Period[]).map((p) => (
            <button key={p} onClick={() => setPeriod(p)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${period === p ? "bg-accent text-white" : "text-muted hover:bg-surface-hover"}`}>
              {p === "daily" ? t("radar.daily", "일간") : p === "week" ? t("radar.week", "주간") : t("radar.month", "월간")}
            </button>
          ))}
        </div>
        {period === "daily" && (
          <label className="flex items-center gap-1.5 text-xs text-muted">
            {t("radar.dateLabel", "기준일")}
            <select value={date} onChange={(e) => setDate(e.target.value)}
              className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink">
              {!dates.length && <option value="">—</option>}
              {dates.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
        )}
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-muted">
        <label className="flex items-center gap-1.5">
          {t("radar.tax", "분류")}
          <select value={taxFilter} onChange={(e) => setTaxFilter(e.target.value)}
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink">
            {TAXONOMIES.map((x) => <option key={x} value={x}>{x === "all" ? t("radar.all", "전체") : x}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          {t("radar.minStars", "최소 스타")} <span className="w-10 text-right">{fmtStars(minStars)}</span>
          <input type="range" min={0} max={50000} step={500} value={minStars}
            onChange={(e) => setMinStars(Number(e.target.value))}
            className="w-32 accent-accent" />
        </label>
      </div>

      <div className="grid gap-3 lg:grid-cols-[220px_1fr]">
        <Card title={t("radar.topics", "관심 주제")} className="self-start">
          <div className="mb-1 text-[11px] text-muted">{t("radar.topicsWindow", "최근 30일")}</div>
          {topicsQ.isLoading && <Spinner />}
          {topicsQ.error && <ErrorBox message={String((topicsQ.error as Error).message)} onRetry={() => topicsQ.refetch()} />}
          {allTopics.length === 0 && !topicsQ.isLoading && (
            <div className="text-xs text-muted">{t("radar.topicsEmpty", "주제 데이터가 없습니다")}</div>
          )}
          <div className="flex flex-col gap-1">
            {allTopics.map((tp) => (
              <button key={tp.topic} onClick={() => selectTopic(tp.topic)}
                className={`flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${topic === tp.topic ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-hover"}`}>
                <span className="truncate"># {tp.topic}</span>
                <span className="shrink-0 text-muted">{tp.count}</span>
              </button>
            ))}
          </div>
        </Card>

        <div className="min-w-0">
          {topic && !repoName && (
            <div className="mb-3">
              <button onClick={clearTopic}
                className="mb-2 text-xs text-accent hover:underline">
                ← {t("radar.backToTrending", "트렌딩으로 돌아가기")}
              </button>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold text-ink">#{topic}</h2>
                <Badge>{t("radar.topicCount", "최근 30일 {count}개 저장소").replace("{count}", String(topicCount))}</Badge>
                <div className="flex gap-1 rounded-lg border border-border bg-surface p-0.5">
                  {["all", ...PLATFORMS].map((p) => (
                    <button key={p} onClick={() => setTopicPlatform(p)}
                      className={`rounded-md px-2 py-0.5 text-[11px] font-medium capitalize transition-colors ${topicPlatform === p ? "bg-accent text-white" : "text-muted hover:bg-surface-hover"}`}>
                      {p === "all" ? t("radar.all", "전체") : p}
                    </button>
                  ))}
                </div>
              </div>
              {related.length > 0 && (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-[11px] font-semibold uppercase text-muted">{t("radar.related", "관련 주제")}</span>
                  {related.map((r) => (
                    <button key={r.topic} onClick={() => selectTopic(r.topic)}
                      className="rounded-full border border-border px-2 py-0.5 text-[11px] text-accent hover:bg-accent-soft">
                      #{r.topic}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {repoName ? (
            repoQ.isLoading ? <Card><Spinner /></Card>
            : repoQ.error || !repoQ.data?.item ? (
              <Card>
                <ErrorBox message={t("radar.detailNotFound", "수집된 저장소가 아닙니다")}
                  onRetry={() => setRepoName(null)} />
                <Button variant="ghost" className="mt-2 text-xs" onClick={() => setRepoName(null)}>
                  ← {t("radar.backToTrending", "트렌딩으로 돌아가기")}
                </Button>
              </Card>
            ) : <RepoDetail item={repoQ.data.item} lang={lang} onBack={() => setRepoName(null)}
                onTopic={selectTopic} onRun={(it) => submitDevReview(it, "detail")}
                runState={runMsg["detail"]} setView={setView} t={t} />
          ) : listLoading ? (
            <Card><Spinner /></Card>
          ) : listError ? (
            <Card><ErrorBox message={(listError as Error).message || t("radar.loadFail", "데이터를 불러오지 못했습니다.")} onRetry={() => { trendingQ.refetch(); rankingQ.refetch(); topicReposQ.refetch(); }} /></Card>
          ) : !items.length ? (
            topic ? (
              <Empty>{t("radar.topicNoRepos", "이 주제의 저장소가 없습니다")} — {t("radar.topicNoReposDesc", "다른 주제를 선택하거나 트렌딩으로 돌아가세요.")}</Empty>
            ) : (
              <Empty>{t("radar.emptyTitle", "아직 수집된 데이터가 없습니다")}<br />
                <span className="text-xs">{t("radar.emptyDesc", "매일 KST 06:00 자동 수집되거나, 수집 버튼으로 수집을 시작하세요.")}</span></Empty>
            )
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {items.map((item, i) => {
                const name = item.repo_full_name || item.repo || "";
                const key = `${name}:${i}`;
                const state = runMsg[key];
                return (
                  <Card key={key} className="flex flex-col">
                    <div className="flex items-start gap-2">
                      <span className="mt-0.5 w-6 shrink-0 text-sm font-bold text-muted">{item.rank ?? ""}</span>
                      <div className="min-w-0 flex-1">
                        <button onClick={() => name && setRepoName(name)}
                          className="block max-w-full truncate text-left text-sm font-semibold text-accent hover:underline">
                          {name}
                        </button>
                        {item.description && (
                          <p className="mt-0.5 line-clamp-2 text-xs text-muted">{item.description}</p>
                        )}
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          {item.platform && <Badge tone="accent">{item.platform}</Badge>}
                          {item.taxonomy && <Badge>{item.taxonomy}</Badge>}
                          {(item.appearances || 1) > 1 && <Badge>{item.appearances}회</Badge>}
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
                          {item.license
                            ? <span title={`License: ${item.license}`}><Badge>{licenseShort(item.license)}</Badge></span>
                            : <Badge>{t("radar.noLicense", "라이선스 없음")}</Badge>}
                          {item.language && <Badge>{item.language}</Badge>}
                          <span>★ {fmtStars(item.stars)}</span>
                          {item.stars_today == null
                            ? <span className="text-muted">—</span>
                            : item.stars_today > 0
                              ? <span className="text-ok">▲ +{fmtStars(item.stars_today)}</span>
                              : <span>±0</span>}
                        </div>
                        {(item.summary || []).length > 0 && (
                          <ul className="mt-2 space-y-0.5">
                            {(item.summary || []).map((l, j) => (
                              <li key={j} className="text-xs text-ink-2">· {l}</li>
                            ))}
                          </ul>
                        )}
                        <div className="mt-2 flex flex-wrap items-center gap-1">
                          {(item.topics || []).slice(0, 4).map((tp) => (
                            <button key={tp} onClick={() => selectTopic(tp)}
                              className="rounded-full border border-border px-1.5 py-0.5 text-[11px] text-accent hover:bg-accent-soft">
                              #{tp}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>
                    <div className="mt-2 flex items-center justify-between gap-2 border-t border-border pt-2">
                      <div className="min-w-0 text-[11px]">
                        {state === "busy" && <span className="text-muted">{t("radar.running", "실행 중…")}</span>}
                        {state === "ok" && (
                          <button onClick={() => setView("office")} className="text-accent hover:underline">
                            {t("radar.runOk", "실행됨 — Agent Office")}
                          </button>
                        )}
                        {state && state !== "busy" && state !== "ok" && (
                          <span className="text-err">{state}</span>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        {name && (
                          <Button variant="secondary" className="px-2 py-1 text-[11px]" disabled={state === "busy"}
                            onClick={() => submitDevReview(item, key)}>
                            {t("radar.devReview", "dev_review 실행")}
                          </Button>
                        )}
                        <a href={safeUrl(item.repo_url || item.url || "")} target="_blank" rel="noopener noreferrer"
                          className="text-[11px] text-accent hover:underline">
                          GitHub ↗
                        </a>
                      </div>
                    </div>
                  </Card>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function RepoDetail({ item, lang, onBack, onTopic, onRun, runState, setView, t }: {
  item: RadarRepoDetail; lang: Lang;
  onBack: () => void; onTopic: (tp: string) => void;
  onRun: (it: RadarRepoDetail) => void; runState?: string;
  setView: (v: "office") => void;
  t: (k: string, f?: string) => string;
}) {
  const summaries = item.summaries || {};
  const langs = Object.keys(summaries);
  const [pane, setPane] = useState<Lang>(langs.includes(lang) ? lang : (langs[0] as Lang) || "ko");
  const repoName = item.repo_full_name || item.repo || "";
  const repoUrl = item.repo_url || item.url || "";
  return (
    <Card>
      <button onClick={onBack} className="mb-3 text-xs text-accent hover:underline">
        ← {t("radar.backToTrending", "트렌딩으로 돌아가기")}
      </button>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <a href={safeUrl(repoUrl)} target="_blank" rel="noopener noreferrer"
            className="text-base font-semibold text-accent hover:underline">
            {repoName}
          </a>
          {item.description && <p className="mt-1 line-clamp-3 text-sm text-muted">{item.description}</p>}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted">
            {item.snapshot_platform && <Badge tone="accent">{item.snapshot_platform}</Badge>}
            {item.collected_date && <span>{t("radar.collected", "수집일")} {item.collected_date}</span>}
            {item.taxonomy && <Badge>{item.taxonomy}</Badge>}
            {item.language && <Badge>{item.language}</Badge>}
            <span>★ {fmtStars(item.stars)}</span>
            {item.license
              ? <span title={`License: ${item.license}`}><Badge>{licenseShort(item.license)}</Badge></span>
              : <Badge>{t("radar.noLicense", "라이선스 없음")}</Badge>}
            {(item.appearances || 1) > 1 && <Badge>{item.appearances}회</Badge>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {repoName && (
            <Button variant="secondary" className="px-2.5 py-1 text-xs" disabled={runState === "busy"}
              onClick={() => onRun(item)}>
              {t("radar.devReview", "dev_review 실행")}
            </Button>
          )}
          <a href={safeUrl(repoUrl)} target="_blank" rel="noopener noreferrer"
            className="text-xs text-accent hover:underline">GitHub ↗</a>
        </div>
      </div>
      {runState === "ok" && (
        <div className="mt-2 text-xs">
          <button onClick={() => setView("office")} className="text-accent hover:underline">
            {t("radar.runOk", "실행됨 — Agent Office")}
          </button>
        </div>
      )}
      {runState && runState !== "busy" && runState !== "ok" && (
        <div className="mt-2 text-xs text-err">{runState}</div>
      )}

      <div className="mt-4">
        <h3 className="mb-2 text-xs font-semibold uppercase text-muted">{t("radar.summary", "요약")}</h3>
        {langs.length > 1 && (
          <div className="mb-2 flex gap-1">
            {langs.map((lg) => (
              <button key={lg} onClick={() => setPane(lg as Lang)}
                className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${pane === lg ? "bg-accent text-white" : "bg-surface-hover text-muted"}`}>
                {lg === "ko" ? "한국어" : lg === "en" ? "English" : "中文"}
              </button>
            ))}
          </div>
        )}
        <ul className="space-y-1">
          {(summaries[pane] || []).map((l, i) => (
            <li key={i} className="text-sm text-ink-2">· {l}</li>
          ))}
          {!langs.length && <li className="text-sm text-muted">-</li>}
        </ul>
      </div>

      {item.readme_excerpt && (
        <details className="mt-4">
          <summary className="cursor-pointer text-xs font-semibold uppercase text-muted">{t("radar.readme", "README")}</summary>
          <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-surface-hover p-3 text-xs whitespace-pre-wrap">{item.readme_excerpt}</pre>
        </details>
      )}

      {(item.related_topics || []).length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-xs font-semibold uppercase text-muted">{t("radar.related", "관련 주제")}</h3>
          <div className="flex flex-wrap gap-1.5">
            {(item.related_topics || []).map((tp) => (
              <button key={tp} onClick={() => onTopic(tp)}
                className="rounded-full border border-border px-2 py-0.5 text-[11px] text-accent hover:bg-accent-soft">
                #{tp}
              </button>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
