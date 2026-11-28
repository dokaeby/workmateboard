/** 차트 프리미티브 — 외부 차트 라이브러리 없이 SVG/div로 렌더.
 *  Langfuse식 밀도: 최소 장식, 데이터만. */

import { useId } from "react";

interface Pt { label: string; value: number }

/** 일별 비용 SVG area 스파크라인 — points는 날짜순 정렬 가정. */
export function CostArea({ points, height = 140 }: { points: Pt[]; height?: number }) {
  const gid = `office-area-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const W = 600;
  const H = height;
  const pad = 4;
  if (!points.length) return null;
  const max = Math.max(1e-9, ...points.map((p) => p.value));
  const step = points.length > 1 ? (W - pad * 2) / (points.length - 1) : W - pad * 2;
  const coords = points.map((p, i) => ({
    x: pad + i * step,
    y: H - pad - (p.value / max) * (H - pad * 2),
    p,
  }));
  const line = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const area = `${line} L${coords[coords.length - 1].x.toFixed(1)},${H} L${coords[0].x.toFixed(1)},${H} Z`;
  const total = points.reduce((a, p) => a + p.value, 0);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="cost sparkline" preserveAspectRatio="none">
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-accent, #6366f1)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="var(--color-accent, #6366f1)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gid})`} />
        <path d={line} fill="none" stroke="var(--color-accent, #6366f1)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
        {coords.map((c, i) => (
          <circle key={i} cx={c.x} cy={c.y} r="2.5" fill="var(--color-accent, #6366f1)">
            <title>{`${c.p.label} · $${c.p.value.toFixed(4)}`}</title>
          </circle>
        ))}
      </svg>
      <div className="mt-1 flex justify-between text-[10px] text-muted">
        <span>{points[0]?.label}</span>
        <span className="font-medium text-ink">${total.toFixed(2)}</span>
        <span>{points[points.length - 1]?.label}</span>
      </div>
    </div>
  );
}

/** 범용 시계열 area 스파크라인 — 알림 발생 추이 등 count 시리즈용.
 *  CostArea와 달리 총계를 강제하지 않고 valueFmt로 표시값을 포맷한다. */
export function TrendArea({ points, height = 90, valueFmt, empty }: {
  points: Pt[]; height?: number; valueFmt?: (v: number) => string; empty?: string;
}) {
  const gid = `office-trend-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const W = 600;
  const H = height;
  const pad = 4;
  if (!points.length) {
    return empty ? <div className="py-3 text-center text-xs text-muted">{empty}</div> : null;
  }
  const max = Math.max(1e-9, ...points.map((p) => p.value));
  const step = points.length > 1 ? (W - pad * 2) / (points.length - 1) : W - pad * 2;
  const coords = points.map((p, i) => ({
    x: pad + i * step,
    y: H - pad - (p.value / max) * (H - pad * 2),
    p,
  }));
  const line = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const area = `${line} L${coords[coords.length - 1].x.toFixed(1)},${H} L${coords[0].x.toFixed(1)},${H} Z`;
  const fmt = valueFmt ?? ((v: number) => v.toLocaleString());
  const total = points.reduce((a, p) => a + p.value, 0);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="trend sparkline" preserveAspectRatio="none">
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-accent, #2563eb)" stopOpacity="0.3" />
            <stop offset="100%" stopColor="var(--color-accent, #2563eb)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gid})`} />
        <path d={line} fill="none" stroke="var(--color-accent, #2563eb)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between text-[10px] text-muted">
        <span>{points[0]?.label}</span>
        <span className="font-medium tabular-nums text-ink">{fmt(total)}</span>
        <span>{points[points.length - 1]?.label}</span>
      </div>
    </div>
  );
}

export interface BarRow {
  label: string;
  value: number;
  display: string;
  hint?: string;
  tone?: "err" | "warn" | "accent" | "muted";
}

const BAR_CLS: Record<NonNullable<BarRow["tone"]>, string> = {
  err: "bg-err",
  warn: "bg-warn",
  accent: "bg-accent/80",
  muted: "bg-surface-hover",
};

/** 비율 바 리스트 — 오류 분류/기타 카테고리 분해용. value는 최대값 대비 %. */
export function ShareBars({ rows, empty }: { rows: BarRow[]; empty?: string }) {
  if (!rows.length) {
    return empty ? <div className="py-4 text-center text-xs text-muted">{empty}</div> : null;
  }
  const max = Math.max(1e-9, ...rows.map((r) => r.value));
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => {
        const pct = Math.max(1.5, (r.value / max) * 100);
        return (
          <div key={i} className="group" title={r.hint}>
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="min-w-0 truncate font-mono text-ink">{r.label}</span>
              <span className="flex-none text-muted">{r.display}</span>
            </div>
            <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-surface-hover">
              <div className={`h-full rounded-full ${BAR_CLS[r.tone || "accent"]}`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
