/**
 * 표시용 비밀값 마스킹 — spec 013 US2 Event Dock.
 * 백엔드 services/redaction.py(mask_pii)의 프론트 대응 최소 구현.
 * 토큰/키 원문이 화면에 렌더되기 전에 적용한다. 과도한 마스킹보다
 * 로그 가독성을 우선하되 credential 계열은 반드시 가린다.
 */

const PATTERNS: [RegExp, string][] = [
  // JWT (eyJ...header.payload.signature)
  [/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "***JWT***"],
  // Authorization: Bearer <token>
  [/\bBearer\s+[A-Za-z0-9._~+/=-]{6,}/gi, "Bearer ***"],
  // key=value / key: value 형태의 자격증명
  [
    /\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd|pwd|private[_-]?key)\s*[:=]\s*["']?[^\s"'&,;}]+/gi,
    "$1=***",
  ],
  // OpenAI/Anthropic/GitHub/GitLab/Slack/HF/npm 스타일 토큰
  [/\b(sk|pk|rk|xox[baprs]|ghp|gho|ghu|ghs|ghr|glpat|hf|npm)[-_][A-Za-z0-9_-]{10,}/g, "$1-***"],
  // github_pat_, xapp-, AWS AKIA
  [/\b(github_pat|xapp|AKIA)[-_][A-Za-z0-9_-]{8,}/g, "$1-***"],
  // PEM 개인키 블록
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "***PRIVATE KEY***"],
  // Authorization: Basic <b64>
  [/\bBasic\s+[A-Za-z0-9+/=]{8,}/gi, "Basic ***"],
  // URL userinfo (scheme://user:pass@host)
  [/([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)([^/@\s:]+):([^/@\s]+)@/g, "$1***:***@"],
];

export function maskSecrets(input: string | null | undefined): string {
  let s = String(input ?? "");
  for (const [re, repl] of PATTERNS) s = s.replace(re, repl);
  return s;
}
