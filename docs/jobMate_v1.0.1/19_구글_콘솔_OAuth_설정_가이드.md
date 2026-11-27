# jobMate Google Cloud 콘솔 OAuth 설정 가이드

| 항목 | 값 |
|---|---|
| 문서 버전 | 1.0.1 |
| 기준일 | 2026-11-27 |
| 대상 | 서비스 운영자, 배포 담당자 |
| 상태 | workmateboard 프로젝트에 실제 적용 완료(2026-11-27) |

## 문서 이력

| 버전 | 기준일 | 수정 내용 |
|---|---|---|
| 1.0.1 | 2026-11-27 | 최초 작성 — Gmail API 인증 메일 발송 + Google 로그인 설정, 실측 트러블슈팅 포함 |

## 1. 목적과 범위

이 문서는 jobMate의 다음 두 기능에 필요한 Google Cloud 콘솔 설정을 다룬다.

- **이메일 인증 메일 발송** — 회원가입 시 인증 링크를 Gmail API로 발송
  (`email_verifications` 테이블, `mailer.py`, `/api/v1/auth/verify`, `/api/v1/auth/verify/resend`)
- **Google 로그인** — `/api/v1/auth/google` + `/api/v1/auth/google/callback`

Gmail API 외 경로로는 SMTP 설정(`JOBMATE_SMTP_*`)도 가능하며 OAuth 절차 없이 바로 사용할 수 있다(6장).

### 1.1 현재 프로젝트 정보 (workmateboard)

| 항목 | 값 |
|---|---|
| GCP 프로젝트 | `workmateboard` (프로젝트 번호 `163066629390`) |
| 웹 애플리케이션 클라이언트 | `163066629390-bm1vr3g2gc44u4t9horeu7pckjug010u.apps.googleusercontent.com` |
| 데스크톱 앱 클라이언트 | `163066629390-6tlq2o21ju8kfpdat0rmofhvptno9tqq.apps.googleusercontent.com` |
| 게시 상태 | 테스트 중 (테스트 사용자 3명) |
| 등록 스코프 | `gmail.readonly`, `gmail.send` |

> 클라이언트 ID는 비밀이 아니지만 client_secret·토큰은 절대 문서/커밋에 넣지 않는다.

## 2. 사전 준비

1. [Google Cloud 콘솔](https://console.cloud.google.com)에 로그인한다.
2. jobMate용 GCP 프로젝트를 선택하거나 새로 만든다.
3. **API 및 서비스 → 라이브러리**에서 **Gmail API**를 "사용 설정"한다.
   - 캘린더 연동(`calendar`)도 쓸 경우 **Google Calendar API**도 함께 활성화한다.

## 3. Google 인증 플랫폼 설정

콘솔 좌측 **Google 인증 플랫폼**(구 "OAuth 동의 화면", `/apis/credentials/consent`는 `/auth/overview`로 리다이렉트됨)에서 순서대로 설정한다.

> ⚠️ **UI 주의**: 새 콘솔 UI는 다이얼로그에서 "저장/업데이트"를 눌러도 **페이지 하단의 별도 "저장" 버튼을 한 번 더 눌러야 실제 반영**된다. 다이얼로그만 닫으면 변경이 날아간다(실제로 테스트 사용자·스코프가 이 이유로 유실된 사례 있음).

### 3.1 브랜딩(Branding) — `/auth/branding`

- 앱 이름, 사용자 지원 이메일, 개발자 연락처 이메일을 입력한다.
- 비어 있으면 다음 단계로 진행할 수 없다.

### 3.2 대상(Audience) — `/auth/audience`

- 사용자 유형: **외부(External)**
- 게시 상태: **테스트 중** 유지 권장
  - 테스트 중에는 "테스트 사용자"로 등록된 계정(최대 100명)만 동의할 수 있다.
  - 소수 사용자 운영에는 테스트 상태가 현실적. 일반 공개 시 `gmail.*` 민감 스코프 때문에 Google OAuth 앱 검증 심사가 필요하다.

**테스트 사용자 추가 방법** (실측 기준):

1. "사용자 추가" → 다이얼로그의 이메일 필드에 주소 입력
2. **Enter를 눌러 칩(chip)으로 변환** — 칩이 아니면 저장되지 않는다
3. 이메일을 여러 개 넣을 때 **한 줄에 하나씩** 입력한다. 여러 줄을 한 번에 붙여넣으면 칩 파싱이 깨진다(실제로 `doka` 같은 잘못된 칩이 생성된 사례 있음)
4. "저장" 클릭 → 다이얼로그 닫힘 → 목록에 "사용자 N명(테스트 N명)"으로 표시되는지 확인

현재 등록된 테스트 사용자: `dokaeby@gmail.com`, `dokaeby0@gmail.com`, `dokaeby00@gmail.com`

### 3.3 데이터 액세스(Data Access / Scopes) — `/auth/scopes`

"범위 추가 또는 삭제" → "직접 범위 붙여넣기"에 스코프 URL을 넣고 "테이블에 추가" → 행 선택 상태로 "업데이트" → **페이지 하단 "저장"** 순서로 진행한다.

| 용도 | 스코프 |
|---|---|
| 인증 메일 발송 | `https://www.googleapis.com/auth/gmail.send` |
| 메일 조회(선택) | `https://www.googleapis.com/auth/gmail.readonly` |
| 캘린더 연동(선택) | `https://www.googleapis.com/auth/calendar.events` |

참고: `gmail.send`만으로 발송은 가능하다. `gmail.readonly`는 메일 본문 수집 워크플로(`JOBMATE_GMAIL_QUERY` 폴링)를 쓸 때만 필요하다. 현재 둘 다 등록돼 있다.

### 3.4 클라이언트(Clients) — `/auth/clients` — 용도별 2개 필요

#### (1) 데스크톱 앱 클라이언트 — 토큰 발급용

`jobmate google-auth` 명령이 Gmail/Calendar API 토큰을 발급할 때 사용한다.

1. **클라이언트 만들기 → 애플리케이션 유형: 데스크톱 앱**
2. 생성 직후 표시되는 **JSON 다운로드**로 `client_secret_*.json`을 받는다.
   - `client_id`, `client_secret`, `redirect_uris: ["http://localhost"]`가 들어 있다.
   - **대화상자를 닫으면 client_secret을 다시 볼 수 없다** — 반드시 다운로드한다.
3. 프로젝트 루트에 `credentials.json`으로 저장한다(`.gitignore`의 `credentials*.json` 패턴으로 자동 제외).

#### (2) 웹 애플리케이션 클라이언트 — Google 로그인용

1. **클라이언트 만들기 → 애플리케이션 유형: 웹 애플리케이션**
2. **승인된 리디렉션 URI**에 환경별 주소를 정확히 등록한다.

   ```
   https://<프로덕션 도메인>/api/v1/auth/google/callback
   http://localhost:8000/api/v1/auth/google/callback
   http://127.0.0.1:8000/api/v1/auth/google/callback
   ```

   - 현재 등록값: `https://workmateboard.vercel.app/api/v1/auth/google/callback`, `http://localhost:8000/...`, `http://127.0.0.1:8000/...`
   - 로컬 포트가 8017 등으로 다르면 해당 포트도 별도 URI로 추가해야 한다.
   - 클라이언트 ID·보안 비밀번호를 `.env`에 설정한다(4장).

## 4. 토큰 발급과 환경변수

### 4.1 Gmail API 토큰 발급

```bash
uv run jobmate google-auth gmail \
  --credentials credentials.json \
  --token-path data/token_gmail.json
```

동작 순서:

1. 앱이 `http://127.0.0.1:<랜덤포트>/`에 임시 리스너를 열고 동의 URL을 출력·브라우저로 연다
2. 브라우저에서 계정 선택 → "확인되지 않은 앱" 경고 → **계속** → 범위 동의 → **계속**
3. `http://127.0.0.1:<포트>/`로 리다이렉트되며 `data/token_gmail.json`에 토큰 저장

주의:

- **타임아웃 300초** — 동의 화면을 5분 안에 완료해야 한다. URL만 출력되고 브라우저가 안 열리는 환경(SSH 등)에서는 출력된 URL을 직접 브라우저에 붙여넣는다.
- 동의 완료 후 브라우저에 `chrome-error://` 페이지가 보이는 것은 정상이다 — 로컬 리스너가 응답 직후 종료하기 때문. 토큰 파일 생성 여부로 성공을 판단한다.
- 로그인 계정이 테스트 사용자여야 하고, 민감 스코프라 "확인되지 않은 앱" 경고가 반드시 뜬다(테스트 모드에서는 정상).
- 캘린더도 쓸 경우 `calendar`로 동일하게 발급한다.

### 4.2 `.env` 설정

```bash
# Gmail API 토큰 (로컬 파일 경로) — settings.gmail_token_path로 인식
JOBMATE_GMAIL_TOKEN_PATH=data/token_gmail.json

# Vercel 등 서버리스 — 토큰 JSON 전체를 env로 전달(임시 파일 0600으로 materialize)
JOBMATE_GMAIL_TOKEN_JSON=<token_gmail.json 내용>

# Google 로그인(웹) — 웹 애플리케이션 클라이언트의 값
# 비워 두면 /api/v1/auth/google 비활성
GOOGLE_OAUTH_CLIENT_ID=<웹 클라이언트 ID>
GOOGLE_OAUTH_CLIENT_SECRET=<웹 클라이언트 보안 비밀번호>
GOOGLE_OAUTH_REDIRECT_URI=https://<도메인>/api/v1/auth/google/callback
```

- `GOOGLE_OAUTH_*`가 없으면 레거시 `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI`도 인식한다.
- 토큰 어댑터 주입 경로: `_token_path_for()`(api/support.py)가 `JOBMATE_{KIND}_TOKEN_JSON` env 또는 `*_TOKEN_PATH` 파일을 확인 → `GoogleGmail`/`GoogleCalendar` 생성 → `send_verification_email(gmail=adapters["gmail"])`에서 발송.
- 토큰 만료 시 `google-auth`를 다시 실행한다(리프레시 토큰이 유효한 동안 자동 갱신).

## 5. 동작 확인

1. 앱 기동 후 회원가입 → 인증 메일이 발송되는지 확인한다.
   - 발송 실패 시 로그에서 `gmail verification email send failed` 경고를 확인한다.
   - `JOBMATE_SMTP_*`가 설정돼 있으면 SMTP가 우선한다(`mailer.py`: SMTP 설정 시 Gmail API는 사용하지 않음).
2. `/api/v1/auth/google` 접속 → Google 동의 → 콜백 후 로그인 세션 발급 확인.
3. 미인증 비밀번호 계정이 Google 로그인으로 연결될 때 기존 비밀번호 해시가 비워지는지(계정 클레임) 확인한다 — 인증된 비밀번호 계정은 `google_account_conflict`로 거부된다.

## 6. SMTP 대안 (OAuth 없이 메일 발송)

Gmail API/OAuth 검증이 부담되면 SMTP로 인증 메일을 보낼 수 있다.

```bash
JOBMATE_SMTP_HOST=smtp.gmail.com
JOBMATE_SMTP_PORT=587
JOBMATE_SMTP_USER=<발송 계정>
JOBMATE_SMTP_PASS=<앱 비밀번호>
JOBMATE_SMTP_FROM=<발송 주소>
JOBMATE_SMTP_TLS=starttls   # starttls(기본) | ssl | none
```

- Gmail은 2단계 인증 계정의 **앱 비밀번호**가 필요하다(일반 비밀번호 불가).
- OAuth 동의 화면·클라이언트·토큰 발급 절차를 전부 생략할 수 있어 가장 빠른 경로다.
- Google 로그인 기능까지 필요하면 3~4장의 웹 클라이언트 설정은 별도로 필요하다.

## 7. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| "액세스 차단됨: 앱이 Google 인증 절차를 완료하지 않았습니다" | 로그인 계정이 테스트 사용자가 아님, 또는 테스트 사용자 저장이 실제로 안 됨(페이지 하단 저장 누락) | `/auth/audience`에서 "사용자 N명(테스트 N명)" 카운트 확인, 계정 재등록 |
| "확인되지 않은 앱" 경고 | 민감 스코프 + 테스트 모드 — 정상 | 고급/계속으로 진행 |
| 동의 후 `chrome-error://` | 로컬 콜백 리스너가 응답 후 종료 — 정상 | `data/token_gmail.json` 존재 여부로 판단 |
| `oauth timeout: no callback within 300.0s` | 5분 내 동의 미완료 | `google-auth` 재실행, 출력된 URL을 바로 열기 |
| 인증 메일이 안 옴 | 토큰 파일 없음/만료, `*_TOKEN_PATH` 미설정, Gmail API 미활성, SMTP가 설정돼 Gmail API가 무시됨 | `.env` 확인 → `google-auth` 재발급 → 로그 확인 |
| `google_account_conflict` | 이미 인증된 비밀번호 계정에 Google 로그인 시도 | 의도된 동작 — 계정 연결은 별도 절차 |
| Vercel에서 메일 발송 실패 | `JOBMATE_GMAIL_TOKEN_JSON` env 미설정(서버리스엔 파일 없음) | 토큰 JSON 내용을 env로 등록 |

## 8. 보안 주의사항

- `credentials.json`, `data/token_*.json`, `.env`는 절대 커밋하지 않는다(`.gitignore` 보호 패턴: `credentials*.json`, `*token*.json`, `.env*`).
- client_secret·토큰 원문을 로그에 남기지 않는다(AGENTS.md 로그 절대 원칙 — `mailer.py`는 인증 URL의 토큰 쿼리를 마스킹해 출력).
- 테스트 사용자 목록과 민감 스코프(`gmail.*`)는 앱 검증 전까지 외부 공개 불가 — 공개 시 Google의 OAuth 앱 검증 심사 대상.
- 토큰 파일은 서버리스 배포 시 `JOBMATE_*_TOKEN_JSON` env로 전달하고, 파일시스템이 있는 환경에서는 `*_TOKEN_PATH`를 사용한다.
- 토큰 재발급 주기: 리프레시 토큰은 Google 정책상 6개월 미사용 시 만료될 수 있으므로, 메일 발송이 필요한 서비스는 주기적 헬스체크(실제 발송 또는 `users.getProfile` 호출)를 권장한다.
