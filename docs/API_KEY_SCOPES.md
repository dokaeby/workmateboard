# API 키 인증·스코프 가이드 (spec 006 US7 FR-025)

jobMate API는 두 가지 인증 수단을 지원한다.

| 인증 | 방식 | 용도 |
|---|---|---|
| 세션 | `jobmate_session` 쿠키 (로그인) | 사용자 대시보드·개인 설정 전체 |
| 머신 키 | `Authorization: Bearer jm_...` 또는 `X-API-Key` | 스크립트·크론·외부 브릿지 |

## 키 발급·관리

- `POST /api/v1/auth/api-keys` — 발급. **admin 세션, env 키, `api_keys:manage` 스코프 키만** 가능. member 세션은 발급 불가.
- `GET /api/v1/auth/api-keys` — 본인 키 메타데이터 조회(원문·해시 미포함). admin은 전체 조회.
- `DELETE /api/v1/auth/api-keys/{key_id}` — 폐기. 본인 키는 member도 가능.

키 원문(`jm_...`)은 발급 응답에 **1회만** 표시된다. DB에는 해시+prefix만 저장된다.

## 스코프

DB 키는 `{resource}:{read|write}` 스코프를 가진다(deny-by-default).

- `*` — 전체 접근 (admin/env 키 발급 전용, member·manage 키는 발급 불가)
- `admin` — 관리자 API
- `{resource}:read` / `{resource}:write` — 예: `runs:read`, `tasks:write`, `agents:read`
- `api_keys:manage` — 키 관리 전용 (발급·폐기). 이 키로는 elevated 스코프 발급 불가.

첫 경로 세그먼트가 리소스로 매핑된다(`agent-sessions`→`agent_sessions` 등 정규화 적용).

## 세션 전용 엔드포인트 (머신 키 불가)

다음 경로는 세션 인증이 필요하며 API 키로 호출할 수 없다:

- `/api/v1/auth/*` — 가입·로그인·MFA·비밀번호 재설정·계정 삭제·export·동의·키 관리의 **세션 부분**
- `/api/v1/me/*` — 사용자별 대시보드/뷰/온보딩 설정 (spec 006)
- `/api/v1/invitations/*` — 조직 초대 수락/거절

## 프로덕션 문서 비활성화

`JOBMATE_MODE=release` 또는 Vercel 배포에서는 `/docs`, `/redoc`, `/openapi.json`이 비활성화(404)된다. 개발 환경에서는 정상 노출.
