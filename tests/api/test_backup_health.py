"""spec 004 US2/US3 — 백업 자동화 + 헬스/상태.

SQLite tmp DB 기준: 백업 생성→backup_runs 기록→보존 삭제→복원 검증,
암호화 산출물, 실패 기록, 헬스 public/detail 화이트리스트, db-down 전이
알림 dedupe를 검증한다. PG 경로는 pg_dump 부재 시 skip.
"""

import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobmate.api.app import create_app
from jobmate.config import Settings
from jobmate.services import backup as backup_svc
from jobmate.services import health as health_svc
from jobmate.store.db import Store
from tests.integration.helpers import default_outputs, scripted_llm


def _env(monkeypatch):
    monkeypatch.setenv("JOBMATE_API_KEY", "test-api-key")
    for v in (
        "JOBMATE_ALLOWED_EMAILS",
        "VERCEL",
        "JOBMATE_MODE",
        "JOBMATE_ENCRYPTION_KEY",
        "JOBMATE_ADMIN_EMAILS",
        "JOBMATE_SLACK_WEBHOOK_URL",
        "JOBMATE_TELEGRAM_BOT_TOKEN",
        "JOBMATE_TELEGRAM_CHAT_ID",
        "JOBMATE_GMAIL_TOKEN_JSON",
        "JOBMATE_GEMINI_API_KEY",
        "JOBMATE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(v, raising=False)


def _client(monkeypatch, settings=None, store=None):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    db = str(tmp / "t.db")
    settings = settings or Settings(db_path=db, backup_dir=tmp / "backups")
    store = store or Store(db)
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-api-key"})
    return c, store, settings, app


def _mk_run(store) -> str:
    run = store.create_run("dev_review", "1.0", {"task": "t"}, "trace-test")
    return str(run.id)


# ---------- US3: backup ----------


def test_backup_run_success_and_verify(monkeypatch):
    c, store, settings, _ = _client(monkeypatch)
    rid = _mk_run(store)
    store.audit(rid, "system", "test_action", "obj")

    res = backup_svc.run_backup(store, settings)
    assert res["status"] == "success", res
    assert res["file"].startswith("sqlite-") and res["file"].endswith(".db")
    artifact = settings.backup_dir / res["file"]
    assert artifact.is_file() and artifact.stat().st_size > 0

    row = store.get_backup_run(res["run_id"])
    assert row["status"] == "success"
    assert row["file_path"] == res["file"]  # 파일명만 — 절대경로 금지
    assert "/" not in row["file_path"] and "\\" not in row["file_path"]

    # 복원 검증 — runs/audit_events 행 수 >0 필요
    v = backup_svc.verify_latest_backup(store, settings)
    assert v["verify_status"] == "ok", v
    row = store.get_backup_run(res["run_id"])
    assert row["verify_status"] == "ok" and row["verified_at"]

    # API 경로 — admin 게이트 + 목록/수동 실행/검증
    r = c.get("/api/v1/backups")
    assert r.status_code == 200, r.text
    assert any(b["id"] == res["run_id"] for b in r.json()["items"])
    r = c.get(f"/api/v1/backups/{res['run_id']}/verify")
    assert r.status_code == 200 and r.json()["verify_status"] == "ok"
    r = c.post("/api/v1/backups/run")
    assert r.status_code == 200 and r.json()["status"] == "success"


def test_backup_encryption_not_plaintext(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("JOBMATE_ENCRYPTION_KEY", "test-encryption-key-material")
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        db_path=str(tmp / "t.db"),
        backup_dir=tmp / "backups",
        backup_encryption=True,
    )
    store = Store(settings.db_path)
    _mk_run(store)
    res = backup_svc.run_backup(store, settings)
    assert res["status"] == "success", res
    assert res["file"].endswith(".enc")
    data = (settings.backup_dir / res["file"]).read_bytes()
    # Fernet 토큰 — 평문 SQLite 헤더("SQLite format 3")가 노출되면 안 된다
    assert not data.startswith(b"SQLite format 3")
    # 복호화→검증 경로도 동작해야 한다
    v = backup_svc.verify_latest_backup(store, settings)
    assert v["verify_status"] == "ok", v


def test_backup_prune_retention_and_keep(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        db_path=str(tmp / "t.db"),
        backup_dir=tmp / "backups",
        backup_retention_days=14,
        backup_keep_count=2,
    )
    store = Store(settings.db_path)
    bdir = settings.backup_dir
    bdir.mkdir(parents=True)
    # 오래된 파일 3개 + 최신 파일 1개 — keep_count=2면 최신 2개만 남는다
    names = []
    for i in range(3):
        p = bdir / f"sqlite-2020010{i}T000000Z.db"
        p.write_bytes(b"x" * 10)
        old = time.time() - (30 + i) * 86400
        os.utime(p, (old, old))
        names.append(p.name)
    # DB 행도 만들어 pruned 마킹 확인
    for n in names:
        store.create_backup_run(
            backend="sqlite",
            file_path=n,
            status="success",
            started_at=datetime.now(UTC) - timedelta(days=40),
        )
    res = backup_svc.run_backup(store, settings)  # 성공 백업 1개 추가 + prune 실행
    assert res["status"] == "success"
    remaining = sorted(p.name for p in bdir.iterdir() if p.is_file())
    # 오래된 3개는 retention/keep 모두 초과 → 삭제. 남는 것: 신규 성공 백업 1개
    # (오래된 파일이 keep_count 안에 들어갈 수 없으므로)
    assert not any(n in remaining for n in names)
    rows = [r for r in store.list_backup_runs(50) if r["file_path"] in names]
    assert rows and all(r["status"] == "pruned" for r in rows)


def test_backup_failure_records_failed(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    store = Store(str(tmp / "real.db"))
    # backup_dir이 파일 아래 경로 → mkdir 실패 → failed 기록 + 산출물 없음
    bad_dir = tmp / "blocked"
    bad_dir.write_text("not a dir")
    settings = Settings(db_path=str(tmp / "real.db"), backup_dir=bad_dir / "sub")
    res = backup_svc.run_backup(store, settings)
    assert res["status"] == "failed"
    row = store.get_backup_run(res["run_id"])
    assert row["status"] == "failed" and row["error"]
    # 실패 시 산출물 잔여 없음
    assert not (bad_dir / "sub").exists() or not any((bad_dir / "sub").glob("*.db"))


def test_backup_interval_gate(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        db_path=str(tmp / "t.db"),
        backup_dir=tmp / "backups",
        backup_interval_hours=24.0,
    )
    store = Store(settings.db_path)
    _mk_run(store)
    # 첫 호출 — last_attempt 없음 → 실행
    r1 = backup_svc.maybe_run_backup(store, settings)
    assert r1["status"] == "success"
    # 직후 재호출 — interval 미경과 → skip
    r2 = backup_svc.maybe_run_backup(store, settings)
    assert r2.get("skipped") == "interval_not_elapsed"
    # verify 주기 게이트도 동일 패턴
    v1 = backup_svc.maybe_verify_backup(store, settings)
    assert v1["verify_status"] == "ok"
    v2 = backup_svc.maybe_verify_backup(store, settings)
    assert v2.get("skipped") == "interval_not_elapsed"
    # 비활성 설정
    settings0 = Settings(
        db_path=settings.db_path,
        backup_dir=settings.backup_dir,
        backup_interval_hours=0.0,
        backup_verify_interval_hours=0.0,
    )
    assert backup_svc.maybe_run_backup(store, settings0).get("skipped") == "disabled"


@pytest.mark.skipif(not __import__("shutil").which("pg_dump"), reason="pg_dump not installed")
def test_backup_pg_dump_path(monkeypatch):
    pytest.skip("PG DSN fixture 필요 — CI 환경에서만 검증")


# ---------- US2: health ----------


def test_health_public_whitelist(monkeypatch):
    c, store, settings, _ = _client(monkeypatch)
    r = c.get("/api/v1/health/public")
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    body = r.json()
    assert body["status"] in ("ok", "degraded", "down")
    assert "version" in body and "uptime_s" in body
    comps = body["components"]
    assert "api" in comps and "db" in comps
    # 화이트리스트 — 내부 정보 유출 금지
    blob = str(body)
    assert str(settings.db_path) not in blob
    assert "/Users/" not in blob and "data/jobmate" not in blob
    for _name, st in comps.items():
        assert st in ("ok", "degraded", "down", "unknown")
        assert isinstance(st, str)  # status 문자열만 — latency/detail 없음


def test_health_detail_admin_and_components(monkeypatch):
    c, store, settings, _ = _client(monkeypatch)
    # 비인증 → 401
    anon = TestClient(c.app)
    assert anon.get("/api/v1/health/detail").status_code == 401
    r = c.get("/api/v1/health/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    # 기존 필드 유지 + components 추가
    assert "api" in body and "db" in body and "cron" in body
    comps = body["components"]
    for name in ("api", "db", "llm", "disk", "backup", "webhook", "cron"):
        assert name in comps, name
        assert comps[name]["status"] in ("ok", "degraded", "down")
        assert "latency_ms" in comps[name] and "checked_at" in comps[name]


def _health_inbox_count(store, prefix="health:db") -> int:
    rows = store._conn.execute(  # noqa: SLF001 - 테스트 직접 조회
        "SELECT COUNT(*) AS n FROM inbox_items WHERE dedupe_key LIKE ?",
        (f"{prefix}%",),
    ).fetchone()
    return int(rows["n"])


def test_health_db_down_degraded_and_alert_dedupe(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(db_path=str(tmp / "t.db"), backup_dir=tmp / "backups")
    store = Store(settings.db_path)
    _mk_run(store)
    # 풀 스레드의 신규 커넥션만 실패하도록 _path를 깨진 경로로 — 메인 스레드의
    # 기존 커넥션은 살아 있어 get_state/upsert_inbox_item 등 알림 기록은 정상 동작.
    orig_path = store._path  # noqa: SLF001
    store._path = tmp / "missing" / "x.db"  # noqa: SLF001
    try:
        report = health_svc.run_health_checks(store, settings)
        assert report["components"]["db"]["status"] == "down"
        # api는 ok — 핵심 1개 down → 전체 degraded(down은 api+db 동시)
        assert report["status"] in ("degraded", "down")

        # run_health_cycle — 첫 사이클: db down 전이 → 알림 1회
        r1 = health_svc.run_health_cycle(store, settings)
        assert r1["components"]["db"]["status"] == "down"
        assert _health_inbox_count(store) == 1
        # 두 번째 사이클 — 동일 상태 지속 → 중복 알림 없음
        r2 = health_svc.run_health_cycle(store, settings)
        assert r2["components"]["db"]["status"] == "down"
        assert _health_inbox_count(store) == 1
    finally:
        store._path = orig_path  # noqa: SLF001


def test_health_public_db_down_not_leaking(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(db_path=str(tmp / "t.db"), backup_dir=tmp / "backups")
    store = Store(settings.db_path)
    store._closed = True  # noqa: SLF001
    llm = scripted_llm(default_outputs())
    app = create_app(settings=settings, store=store, llm=llm)
    c = TestClient(app)
    r = c.get("/api/v1/health/public")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("degraded", "down")
    assert body["components"]["db"] == "down"
    assert str(settings.db_path) not in str(body)
    store._closed = False  # noqa: SLF001


def test_backup_health_component_stale(monkeypatch):
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        db_path=str(tmp / "t.db"),
        backup_dir=tmp / "backups",
        backup_interval_hours=1.0,
        backup_stale_hours=30.0,
    )
    store = Store(settings.db_path)
    _mk_run(store)
    # 성공 백업 없음 + 백업 활성 → backup 컴포넌트 down
    report = health_svc.run_health_checks(store, settings)
    assert report["components"]["backup"]["status"] == "down"
    # 오래된 성공 백업 → degraded(stale)
    old = datetime.now(UTC) - timedelta(hours=48)
    store.create_backup_run(
        backend="sqlite",
        file_path="sqlite-old.db",
        status="success",
        started_at=old,
    )
    store.finish_backup_run(
        store.latest_backup_any()["id"],
        status="success",
        size_bytes=10,
        file_path="sqlite-old.db",
        finished_at=old,
    )
    report2 = health_svc.run_health_checks(store, settings)
    assert report2["components"]["backup"]["status"] == "degraded"


def test_backup_prune_ignores_non_jobmate_files(monkeypatch):
    """prune은 run_backup 네이밍(`<backend>-<ts>.<ext>[.enc]`)만 삭제 —
    backup_dir이 라이브 데이터 디렉터리와 겹쳐도 무관 파일은 보존."""
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        db_path=str(tmp / "t.db"),
        backup_dir=tmp / "backups",
        backup_retention_days=0,
        backup_keep_count=0,
    )
    store = Store(settings.db_path)
    bdir = settings.backup_dir
    bdir.mkdir(parents=True)
    decoys = [
        "jobmate.db",  # 라이브 DB가 backup_dir에 놓인 경우
        "other.sqlite",
        "notes.dump",
        "sqlite-backup.db",  # 타임스탬프 없는 유사 이름
        "postgresql-x.dump",
        "sqlite-20200101T000000Z.txt",
    ]
    for n in decoys:
        (bdir / n).write_bytes(b"x" * 10)
    victims = [
        "sqlite-20200101T000000Z.db",
        "postgresql-20200102T000000Z.dump",
        "sqlite-20200103T000000Z.db.enc",
    ]
    for n in victims:
        (bdir / n).write_bytes(b"x" * 10)
    res = backup_svc.prune_backups(store, settings)
    assert res["deleted"] == len(victims), res
    for n in decoys:
        assert (bdir / n).is_file(), n
    for n in victims:
        assert not (bdir / n).exists(), n
    store.close()


def test_public_health_ttl_cache(monkeypatch):
    """공개 헬스는 TTL(15s) 내 재호출 시 run_health_checks를 재실행하지
    않는다 — 비인증 DoS 증폭 차단."""
    _env(monkeypatch)
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(db_path=str(tmp / "t.db"), backup_dir=tmp / "backups")
    store = Store(settings.db_path)
    calls = {"n": 0}
    orig = health_svc.run_health_checks

    def counting(s, st):
        calls["n"] += 1
        return orig(s, st)

    monkeypatch.setattr(health_svc, "run_health_checks", counting)
    health_svc.public_health(store, settings)
    health_svc.public_health(store, settings)
    health_svc.public_health(store, settings)
    assert calls["n"] == 1
    # 다른 Store 인스턴스는 별도 캐시 엔트리 — 재실행된다
    store2 = Store(str(tmp / "t2.db"))
    health_svc.public_health(store2, settings)
    assert calls["n"] == 2
    store2.close()
    store.close()
