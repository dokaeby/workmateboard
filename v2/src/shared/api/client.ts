/**
 * jobMate API 클라이언트 — fetch 단일 진입점.
 * 인증: 세션 쿠키(자동) + localStorage 'jobmate_api_key' → X-API-Key.
 * 모든 뷰/기능은 반드시 이 모듈의 api()/apiGet()/apiPost()를 사용한다.
 */

const API_BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function apiHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  const key = localStorage.getItem("jobmate_api_key") || "";
  if (key) headers["X-API-Key"] = key;
  return headers;
}

export async function api<T = unknown>(
  path: string,
  opts: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, headers, ...rest } = opts;
  const h = apiHeaders(headers as Record<string, string> | undefined);
  let body = rest.body;
  if (json !== undefined) {
    h["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  let r: Response;
  try {
    r = await fetch(API_BASE + path, { ...rest, headers: h, body });
  } catch {
    throw new ApiError(0, "network", "네트워크 연결을 확인하세요 — 인터넷 연결 상태를 확인한 뒤 다시 시도해주세요");
  }
  if (r.status === 401) {
    // 세션 만료 — 앱 셸이 auth 상태로 전환하도록 이벤트 발생.
    // /auth/sessions는 쿠키 세션 전용 엔드포인트라 X-API-Key 인증에서는
    // 항상 401이 정상 — 전역 로그아웃 이벤트에서 제외한다.
    if (!path.startsWith("/auth/sessions")) {
      window.dispatchEvent(new CustomEvent("jm:unauthorized"));
    }
    throw new ApiError(401, "unauthorized", "로그인이 필요합니다 — 새로고침 후 로그인해주세요");
  }
  const text = await r.text();
  let data: unknown = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = {}; }
  }
  if (!r.ok) {
    const j = data as { error?: { code?: string; message?: string } | string };
    const errObj = typeof j?.error === "object" ? j.error : undefined;
    const msg = errObj?.message || (typeof j?.error === "string" ? j.error : "") || r.statusText;
    const code = errObj?.code || "";
    if (r.status === 429) {
      throw new ApiError(429, code || "rate_limited",
        (msg ? msg + " — " : "") + "요청이 너무 많습니다. 잠시 후 다시 시도해주세요");
    }
    throw new ApiError(r.status, code, msg || `요청 실패 (${r.status})`);
  }
  return data as T;
}

export const apiGet = <T = unknown>(path: string) => api<T>(path);
export const apiPost = <T = unknown>(path: string, json?: unknown) =>
  api<T>(path, { method: "POST", json });
export const apiPatch = <T = unknown>(path: string, json?: unknown) =>
  api<T>(path, { method: "PATCH", json });
export const apiDelete = <T = unknown>(path: string) => api<T>(path, { method: "DELETE" });
