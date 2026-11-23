# workmateboard 보안 검사 및 수정 — 2026-11-23

검사 대상은 `C:\Users\Playdata\aio4\workmateboard`의 API, 인증, 데이터 저장,
외부 전송, 도구 실행 및 Python 의존성이다. 상위 저장소의
`.agents/skills/security-audit/SKILL.md`를 적용하고 별도 읽기 전용 검토를 병행했다.
실제 외부 서비스 발송, 운영 데이터 변경, 배포, 커밋/푸시는 수행하지 않았다.

## 확인한 문제와 수정

| 문제 | 수정 |
| --- | --- |
| 공개 비밀번호 가입으로 API 키 보호 우회 및 허용 이메일 사칭 | 보호 환경의 가입에 관리자 API 키 요구 |
| Vercel에서 API 키 미설정 시 익명 접근, 허용 목록 단독 설정 시 익명 접근 | 인증을 닫힌 상태로 처리; 로컬 무보호 개발 모드만 유지 |
| 허용 목록에서 제외한 사용자의 기존 세션 유효 | 요청마다 허용 이메일 재검증 |
| Vercel 외 서버의 cron tick 인증 누락 | 설정된 cron secret/API 키 검증; Vercel 전용 secret 유지 |
| Google 미검증 이메일 및 다른 subject 계정 연결 | `email_verified is True`, 보호 환경의 명시적 허용 목록, subject 일치 확인 |
| 임의 Push URL을 통한 SSRF 및 실패 로그의 capability URL 노출 | HTTPS/443 Push 서비스 호스트 제한, 저장 구독 재검증, 리디렉션 차단, 10초 제한, 예외 유형만 로깅 |
| 파일 읽기의 형제 경로 접두사/심볼릭 링크 이탈 | 실제 경로 해석 후 저장소 포함 여부 검사, 읽기 크기 제한 |
| `deep-translator==1.11.4`의 PYSEC-2022-252 권고 | 의존성과 lock 항목 제거; MyMemory 폴백을 기존 HTTPX로 구현; 실패 시 원문 보존 |
| SQLite 공유 연결의 동시 조회 누락/InterfaceError | 공유 prepared statement 캐시 비활성화(CPython #118172) |

Windows에서 실패하던 테스트 데이터의 UTF-8 인코딩 및 POSIX 권한 비트 검사를
플랫폼에 맞게 수정했다. UI 검사는 데모를 명시적으로 끄고, 즉시 사라지는 메시지 대신
카드 제거와 저장 상태로 승인/거절 완료를 확인한다. 보호 환경 테스트에는 관리자 키를 제공한다.

## 검사 방법과 해석

- 스킬 명령: `npx @claude-flow/cli security scan --depth full --target <project>`.
  `full`은 현재 CLI에서 `deep`으로 처리된다. 실행한 검사에서 탐지 0건이지만,
  이 Python 프로젝트에는 package.json이 없어 의존성 검사가 생략되었다(종료 코드 1).
  따라서 이 결과를 전체 보안 통과로 해석하지 않았다.
- `pip-audit`: requirements.txt와 uv.lock을 별도로 검사했다. 기존 requirements.txt에는
  deep-translator가 빠져 있어 해당 검사만으로는 발견되지 않았다. uv.lock 전체 검사에서
  권고 1건을 확인했고, 제거 후 99개 registry 패키지의 알려진 취약점은 0건이다.
- requirements.txt를 uv.lock에서 다시 내보내 기존에 누락된 Postgres 체크포인터와
  연결 풀도 배포 의존성에 포함했다.
- Bandit: high 0, medium 2, low 17. 남은 medium은 Vercel의 `/tmp` SQLite 위치(B108)와
  화이트리스트로 구성하는 UPDATE 열 이름(B608)이다. SQL 값은 바인딩하며 열 이름은
  고정 허용 집합에서 선택한다. 나머지는 subprocess 사용과 빈 기본값/상태 상수 등이다.
  경고를 일괄 숨기거나 모두 취약점으로 집계하지 않았다.
- 최초 보안 재현: 수정 전 17 실패/1 통과/1 건너뜀, 수정 후 같은 테스트는 18 통과/1 건너뜀.
  허용 목록·Push 호환·Google 정책·동시 조회 회귀도 추가했다.
- SQLite 동시 조회 테스트는 수정 전 사용자 조회 누락으로 실패하고, 캐시 비활성화 후 통과했다.

## 검증 환경과 남은 제한

- Python 3.12.14의 격리된 검사 환경. Windows 애플리케이션 제어가 최신 일부 DLL을
  차단해 테스트에만 LangGraph 0.6.11, checkpoint 2.1.2, sqlite checkpoint 2.0.11,
  postgres checkpoint 2.0.23, psycopg/binary 3.2.10, ormsgpack 1.11.0, xxhash 3.6.0을
  사용했다. 운영 pyproject/uv.lock을 이 버전들로 낮추지 않았다.
  운영 lock 버전 그대로의 실행은 별도 환경에서 재검증해야 한다.
- mypy는 Windows가 차단한 compiled wheel 대신 소스 설치한 1.18.2로 실행했다.
- 실제 Google OAuth, Push 서비스, PostgreSQL 서버 및 외부 번역 서비스의 실연동은 검증하지 않았다.
- 심볼릭 링크 테스트는 Windows 생성 권한이 없어 건너뛰었다. 경로 접두사 이탈은 실행 검증했다.
- Windows 토큰 파일의 NTFS ACL은 확인하지 않았다. POSIX의 0600과 동일하다고 주장하지 않는다.
- `graphify update .`는 Windows 애플리케이션 제어 정책이 실행 파일을 차단해 실행하지 못했다.
- 기존 계정/세션은 자동 삭제하지 않았다. 이전 공개 가입 기간의 계정은 관리자가 확인하고,
  `JOBMATE_ALLOWED_EMAILS`를 설정해야 한다. 이미 허용 이메일을 사칭해 생성된 계정이 있다면
  별도 계정 정리가 필요하다. 비 Vercel 외부 배포에서도 API 키/허용 목록을 설정해야 한다.

## 근거

- 공급망 권고: https://osv.dev/vulnerability/PYSEC-2022-252
  (현재 권고 범위에 1.11.4가 포함되고 수정 버전이 제시되지 않음; 설치본의 악성 동작을
  직접 확인했다는 의미는 아님.)
- SQLite 동시 캐시 문제: https://github.com/python/cpython/issues/118172
- Apple Push 호스트: https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers
- Mozilla Push endpoint: https://mozilla-services.github.io/autopush-rs/http.html
- Windows Push 호스트: https://learn.microsoft.com/en-us/windows/apps/develop/notifications/push-notifications/firewall-allowlist-config

## 최종 검증 결과

- 격리 worktree 전체 테스트: **214 passed, 4 skipped**, 58.74초.
- Ruff: 전체 검사 통과, 86개 파일 포맷 검사 통과.
- mypy: 51개 소스 파일 오류 없음.
- `git diff --check`: 통과.
- uv.lock의 99개 registry 패키지: 알려진 취약점 0건.
- 독립 읽기 전용 검토: 수정 범위에 추가 차단 결함 없음.
