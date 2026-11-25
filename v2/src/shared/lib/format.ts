/** 표시 포맷 헬퍼 — 기존 index.html의 officeFmtTs/jmFmtAge/usd와 동일 규칙. */

export function fmtAge(sec: number | null | undefined): string {
  const s = Number(sec);
  if (!isFinite(s) || s < 0) return "-";
  if (s < 60) return "방금 전";
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

export function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtElapsed(startIso: string | null | undefined, endIso?: string | null): string {
  if (!startIso) return "-";
  const s = new Date(startIso).getTime();
  const e = endIso ? new Date(endIso).getTime() : Date.now();
  if (isNaN(s) || isNaN(e)) return "-";
  const sec = Math.max(0, Math.floor((e - s) / 1000));
  if (sec < 60) return `${sec}초`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분 ${sec % 60}초`;
  return `${Math.floor(sec / 3600)}시간 ${Math.floor((sec % 3600) / 60)}분`;
}

export function usd(v: number | null | undefined): string {
  const n = Number(v);
  if (!isFinite(n)) return "$0.00";
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

/** URL 컨텍스트 검증 — javascript:/data:/프로토콜상대 URL 차단 (XSS 방어). */
export function safeUrl(u: string | null | undefined): string {
  const s = String(u ?? "").trim();
  if (!s) return "#";
  if (s.startsWith("/") && !s.startsWith("//")) return s;
  if (s.startsWith("./") || s.startsWith("../") || s.startsWith("#") || s.startsWith("?")) return s;
  try {
    const url = new URL(s, window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch { /* fallthrough */ }
  return "#";
}

export function jsonSummary(v: unknown, max = 300): string {
  try {
    const s = JSON.stringify(v, null, 0);
    return s.length > max ? s.slice(0, max) + "…" : s;
  } catch {
    return String(v ?? "");
  }
}
