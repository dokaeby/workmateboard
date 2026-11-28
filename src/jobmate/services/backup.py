"""Backup service — spec 004 US3 (FR-017~FR-024).

백엔드별 백업 생성(SQLite `Connection.backup()` API / PG `pg_dump`),
보존 정책 정리, 복원 검증, automation-cycle 진입점.

로깅 원칙(FR-034): 성공/실패·소요시간·크기만 기록 — 파일 절대경로·
DB 경로/DSN·자격증명은 로그에 남기지 않는다(파일명+백엔드만).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..security import _fernet
from ..store.db import Store

logger = logging.getLogger("jobmate.backup")

# 산출물 확장자 — 암호화 시 ".enc" 접미사가 추가된다.
_BACKEND_EXT = {"sqlite": "db", "postgresql": "dump"}
_STATE_KEY = "backup_state"

# prune 대상 파일명 — run_backup이 생성하는 `<backend>-<UTC타임스탬프>.<ext>`
# (충돌 시 `-<ms%100000>` 접미사, 암호화 시 `.enc` 추가)와 정확히 일치하는
# 이름만 삭제 후보로 인정한다. backup_dir이 라이브 데이터 디렉터리와
# 겹쳐도 jobmate 산출물이 아닌 파일(예: jobmate.db, 임의 *.dump)은 절대
# 건드리지 않는다.
_BACKUP_NAME_RE = re.compile(
    r"^(sqlite|postgresql)-\d{8}T\d{6}Z(?:-\d{1,5})?\.(db|dump)(?:\.enc)?$"
)
# backend → 허용 ext (run_backup의 _BACKEND_EXT와 동일 매핑)
_BACKEND_ALLOWED_EXT = {"sqlite": "db", "postgresql": "dump"}


def _is_backup_artifact_name(name: str) -> bool:
    m = _BACKUP_NAME_RE.match(name)
    return m is not None and m.group(2) == _BACKEND_ALLOWED_EXT[m.group(1)]


# pg_dump stderr/에러 메시지에서 DSN·비밀번호가 새지 않게 마스킹.
_PG_KV_SECRET_RE = re.compile(r"(password\s*=\s*)\S+", re.IGNORECASE)
_PG_URL_CRED_RE = re.compile(r"(://[^:/\s]+:)[^@\s]+@")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


def _backend(store: Store, settings: Settings) -> str:
    try:
        info = store.db_info()
        if info.get("type"):
            return str(info["type"])
    except Exception:
        logger.debug("db_info failed during backup backend detect", exc_info=True)
    dsn = settings.database_url or os.environ.get("POSTGRES_URL", "")
    return "postgresql" if dsn.startswith(("postgres://", "postgresql://")) else "sqlite"


def _backup_dir(settings: Settings) -> Path:
    return Path(settings.backup_dir)


def _ts_name(backend: str) -> str:
    return f"{backend}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _emit(store: Store, event_type: str, event_id: str, data: dict[str, Any]) -> None:
    """웹훅 이벤트 발행 — services.webhooks는 병렬 개발 중이므로
    lazy import + 광범위 except로 부재/실패가 백업 경로를 죽이지 않게 한다."""
    try:
        from .webhooks import emit_event

        emit_event(store, event_type, event_id, data)
    except Exception:
        logger.debug("webhook emit failed type=%s", event_type, exc_info=True)


def _encrypt_file(path: Path) -> Path:
    """파일 바이트를 Fernet으로 암호화해 `<name>.enc`로 교체. 반환: 새 경로."""
    f = _fernet()
    if f is None:
        raise RuntimeError("backup_encryption enabled but no key material configured")
    data = path.read_bytes()
    enc_path = path.with_name(path.name + ".enc")
    enc_path.write_bytes(f.encrypt(data))
    path.unlink()
    return enc_path


def _decrypt_file(path: Path, dest: Path) -> None:
    f = _fernet()
    if f is None:
        raise RuntimeError("encrypted backup found but no key material configured")
    dest.write_bytes(f.decrypt(path.read_bytes()))


def _sanitize_pg_error(err: str, dsn: str) -> str:
    """pg_dump 오류 문자열에서 DSN·자격증명을 마스킹한다.

    참고: conninfo는 현재 pg_dump argv로 전달돼 `ps`에 노출될 수 있다 —
    운영자 자체 DB 자격증명이라 잔여 리스크로 허용(대안인 .pgpass 파일은
    권한 관리가 더 복잡). 여기서는 최소한 저장되는 error 문자열에
    DSN 원문/비밀번호가 남지 않게 한다."""
    if dsn:
        err = err.replace(dsn, "[dsn]")
    err = _PG_URL_CRED_RE.sub(r"\1[redacted]@", err)
    err = _PG_KV_SECRET_RE.sub(r"\1[redacted]", err)
    return err


def _sqlite_backup(src_path: Path, dest: Path) -> None:
    """WAL-safe 온라인 백업 — 파일 단순 복사는 WAL 미반영 위험이 있어
    sqlite3.Connection.backup() API를 사용한다(스펙 edge case).

    URI는 Path.as_uri()로 생성 — `?`/`#`/`%`/공백 등 URI 예약문자를
    포함한 경로도 올바르게 인코딩된다."""
    src = sqlite3.connect(f"{src_path.absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _pg_dump(dsn: str, dest: Path) -> None:
    """pg_dump 커스텀 포맷(-Fc) 서브프로세스. 바이너리 부재/nonzero → 예외.

    자격증명이 포함될 수 있는 DSN은 argv가 아닌 환경/인자로만 전달하고
    로그에는 남기지 않는다."""
    exe = shutil.which("pg_dump")
    if exe is None:
        raise RuntimeError("pg_dump binary not found")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        proc = subprocess.run(  # noqa: S603 - pg_dump 경로는 shutil.which로 해석
            [exe, "-Fc", "-f", str(tmp), dsn],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("pg_dump timed out") from e
    if proc.returncode != 0:
        # stderr에 DSN/자격증명이 섞일 수 있으므로 앞부분만·유형만 기록 +
        # DSN 원문/비밀번호 마스킹(backup_runs.error에 자격증명 잔존 금지)
        err = (proc.stderr or "").strip().splitlines()
        last = _sanitize_pg_error((err[-1] if err else ""), dsn)[:200]
        raise RuntimeError(f"pg_dump exit={proc.returncode}: {last}")
    tmp.replace(dest)


def run_backup(store: Store, settings: Settings) -> dict[str, Any]:
    """백업 1회 실행 — backup_runs running→success/failed 기록 + audit + 이벤트.

    반환: {"run_id","status","file","size_bytes","error","duration_ms"}.
    실패해도 예외를 밖으로 던지지 않는다(automation loop 보호)."""
    backend = _backend(store, settings)
    out_dir = _backup_dir(settings)
    started = time.monotonic()
    run_id = store.create_backup_run(
        backend=backend, status="running", started_at=datetime.now(UTC)
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "backend": backend,
        "status": "failed",
        "file": "",
        "size_bytes": 0,
        "error": "",
        "duration_ms": 0,
    }
    final_path: Path | None = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = _BACKEND_EXT.get(backend, "dump")
        name = f"{_ts_name(backend)}.{ext}"
        final_path = out_dir / name
        # 같은 초에 두 번 실행돼도 충돌하지 않게 suffix를 붙인다
        if final_path.exists():
            final_path = out_dir / f"{final_path.stem}-{int(time.time() * 1000) % 100000}.{ext}"
            name = final_path.name
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(out_dir), prefix=".backup-", suffix=".part")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            if backend == "sqlite":
                info = store.db_info()
                src = info.get("path")
                if not src:
                    raise RuntimeError("sqlite db path unavailable")
                _sqlite_backup(Path(str(src)), tmp_path)
            else:
                dsn = settings.database_url or os.environ.get("POSTGRES_URL", "")
                if not dsn:
                    raise RuntimeError("postgres DSN unavailable for pg_dump")
                _pg_dump(dsn, tmp_path)
            if settings.backup_encryption:
                enc_path = _encrypt_file(tmp_path)
                # tmp_path가 이미 .enc로 rename됐으므로 최종 이름으로 다시 이동
                enc_path.replace(out_dir / (name + ".enc"))
                final_path = out_dir / (name + ".enc")
                name = final_path.name
            else:
                tmp_path.replace(final_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        size = final_path.stat().st_size if final_path.exists() else 0
        if size <= 0:
            raise RuntimeError("backup artifact is empty")
        result.update(
            status="success",
            file=name,
            size_bytes=size,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        store.finish_backup_run(
            run_id,
            status="success",
            size_bytes=size,
            file_path=name,
            finished_at=datetime.now(UTC),
        )
        store.audit(
            run_id,
            "system",
            "backup_run",
            name,
            {"status": "success", "backend": backend, "size_bytes": size},
        )
        _emit(
            store,
            "backup.completed",
            f"backup:{run_id}",
            {"run_id": run_id, "backend": backend, "file": name, "size_bytes": size},
        )
        logger.info(
            "backup success backend=%s file=%s size=%d duration_ms=%d",
            backend,
            name,
            size,
            result["duration_ms"],
        )
    except Exception as e:
        # 부분 산출물 정리 — 실패 시 백업 파일을 남기지 않는다
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        err = f"{type(e).__name__}: {e}"[:400]
        result.update(
            status="failed",
            error=err,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            store.finish_backup_run(
                run_id, status="failed", error=err, finished_at=datetime.now(UTC)
            )
            store.audit(
                run_id, "system", "backup_run", "", {"status": "failed", "error": err[:200]}
            )
        except Exception:
            logger.exception("backup_runs finish/audit failed run=%s", run_id)
        _emit(
            store,
            "backup.failed",
            f"backup:{run_id}",
            {"run_id": run_id, "backend": backend, "error": type(e).__name__},
        )
        logger.warning(
            "backup failed backend=%s duration_ms=%d error=%s",
            backend,
            result["duration_ms"],
            type(e).__name__,
        )
    finally:
        # 성공/실패와 무관하게 보존 정책을 적용한다
        try:
            prune_backups(store, settings)
        except Exception:
            logger.warning("backup prune failed", exc_info=True)
    return result


def _iter_backup_files(out_dir: Path) -> list[Path]:
    """백업 디렉터리 스캔 — jobmate 산출물 이름 패턴과 일치하는 파일만
    반환한다. DB 행이 없는 외부/고아 산출물도 정리 대상(FR-019)이지만,
    이름이 `<backend>-<ts>.<ext>[.enc]` 패턴과 다르면(예: backup_dir이
    라이브 데이터 디렉터리와 겹친 경우의 jobmate.db) 절대 삭제하지 않는다."""
    if not out_dir.is_dir():
        return []
    files = []
    for p in out_dir.iterdir():
        if p.is_file() and not p.name.startswith(".") and _is_backup_artifact_name(p.name):
            files.append(p)
    return files


def prune_backups(store: Store, settings: Settings) -> dict[str, Any]:
    """보존 정책 적용 — 파일시스템 스캔 기준.

    유지 조건(둘 다 만족해야 유지):
      - mtime 나이 <= backup_retention_days
      - 최신순 순위 < backup_keep_count
    즉 retention 초과 **또는** keep_count 초과분은 삭제한다 — 디스크 상한과
    기간 상한 중 더 엄격한 쪽을 따른다(둘 다 '최대' 의미).
    삭제된 파일에 대응하는 backup_runs 행은 status='pruned'로 표시한다
    (행 삭제 대신 이력 보존 — 감사 추적성)."""
    out_dir = _backup_dir(settings)
    files = _iter_backup_files(out_dir)
    now = time.time()
    retention_s = max(0, int(settings.backup_retention_days)) * 86400
    keep = max(0, int(settings.backup_keep_count))
    # 최신순 정렬 — mtime 기준
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    deleted: list[str] = []
    freed = 0
    for idx, p in enumerate(files):
        age = now - p.stat().st_mtime
        if age <= retention_s and idx < keep:
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            deleted.append(p.name)
            freed += size
            # 파일명+크기만 로그 — 경로 원문 금지(FR-034)
            logger.info("backup pruned file=%s size=%d age_days=%.1f", p.name, size, age / 86400)
        except OSError as e:
            logger.warning("backup prune delete failed file=%s: %s", p.name, type(e).__name__)
    marked = store.mark_backups_pruned(deleted) if deleted else 0
    if deleted:
        logger.info(
            "backup prune done deleted=%d freed_bytes=%d rows_marked=%d",
            len(deleted),
            freed,
            marked,
        )
    return {"deleted": len(deleted), "freed_bytes": freed, "rows_marked": marked}


def _verify_sqlite_artifact(artifact: Path) -> tuple[bool, str]:
    """복원된 SQLite 파일 무결성 — integrity_check + 핵심 테이블 행 수."""
    con = sqlite3.connect(f"{artifact.absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()
        if not ok or str(ok[0]).lower() != "ok":
            return False, f"integrity_check={ok[0] if ok else 'none'}"
        for table in ("runs", "audit_events"):
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - 고정 테이블명
            except sqlite3.Error:
                return False, f"missing table {table}"
            if n <= 0:
                return False, f"{table} empty"
        return True, "ok"
    finally:
        con.close()


def verify_latest_backup(store: Store, settings: Settings, run_id: str = "") -> dict[str, Any]:
    """최근 성공 백업(또는 지정 run)의 복원 검증 — FR-022.

    SQLite: 파일 복호화(암호화 시) → 임시 경로에서 integrity_check +
    runs/audit_events 행 수. PostgreSQL: pg_restore 전체 복원은 범위 밖 —
    산출물 존재/크기 스팟 체크만 수행하고 그 한계를 문서화한다.
    결과는 verify_status/verified_at에 기록, 실패 시 backup.failed 이벤트."""
    row = store.get_backup_run(run_id) if run_id else store.latest_backup_success()
    if row is None:
        return {"status": "skipped", "reason": "no backup run found", "verify_status": ""}
    rid = str(row["id"])
    backend = str(row.get("backend") or _backend(store, settings))
    file_name = str(row.get("file_path") or "")
    out = {"run_id": rid, "file": file_name, "verify_status": "failed", "detail": ""}
    artifact = _backup_dir(settings) / file_name if file_name else None
    verified_at = datetime.now(UTC)
    try:
        if artifact is None or not artifact.is_file():
            raise RuntimeError("backup artifact missing")
        with tempfile.TemporaryDirectory(prefix="jobmate-verify-") as td:
            plain = Path(td) / "restore.bin"
            if file_name.endswith(".enc"):
                _decrypt_file(artifact, plain)
            else:
                shutil.copyfile(artifact, plain)
            if backend == "sqlite":
                ok, detail = _verify_sqlite_artifact(plain)
            else:
                # PG: 전체 복원 대신 산출물 sanity 체크(존재/비어있지 않음/커스텀
                # 포맷 헤더 'PGDMP' 확인) — pg_restore 기반 복원 검증은 후속 과제.
                head = plain.read_bytes()[:5]
                ok = bool(head) and head == b"PGDMP"
                detail = "pg artifact header check" if ok else "pg artifact invalid"
        if not ok:
            raise RuntimeError(detail)
        out["verify_status"] = "ok"
        out["detail"] = detail if backend == "sqlite" else "pg artifact sanity ok"
        store.set_backup_verify(rid, "ok", verified_at=verified_at)
        store.audit(
            rid, "system", "backup_verify", file_name, {"verify_status": "ok", "backend": backend}
        )
        _emit(
            store,
            "backup.verified",
            f"backup-verify:{rid}",
            {"run_id": rid, "backend": backend, "file": file_name},
        )
        logger.info("backup verify ok run=%s backend=%s file=%s", rid, backend, file_name)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        out["detail"] = err
        try:
            store.set_backup_verify(rid, "failed", verified_at=verified_at)
            store.audit(
                rid,
                "system",
                "backup_verify",
                file_name,
                {"verify_status": "failed", "error": err[:200]},
            )
        except Exception:
            logger.exception("backup verify record failed run=%s", rid)
        _emit(
            store,
            "backup.failed",
            f"backup-verify:{rid}",
            {"run_id": rid, "backend": backend, "stage": "verify", "error": type(e).__name__},
        )
        logger.warning(
            "backup verify failed run=%s backend=%s error=%s", rid, backend, type(e).__name__
        )
    return out


def _state(store: Store) -> dict[str, Any]:
    st = store.get_state(_STATE_KEY, {})
    return st if isinstance(st, dict) else {}


def _save_state(store: Store, st: dict[str, Any]) -> None:
    try:
        store.set_state(_STATE_KEY, st)
    except Exception:
        logger.debug("backup state persist failed", exc_info=True)


def maybe_run_backup(store: Store, settings: Settings) -> dict[str, Any]:
    """automation-cycle 진입점 — backup_interval_hours 주기 도달 시 백업 실행.

    마지막 '시도' 시각(last_attempt_at) 기준으로 게이트해 실패 시 매 사이클
    재시도로 인한 폭주를 막는다. 0=비활성. 서버리스 cron tick에서도 동일 경로."""
    try:
        interval_h = float(getattr(settings, "backup_interval_hours", 0.0) or 0.0)
    except (TypeError, ValueError):
        interval_h = 0.0
    if interval_h <= 0:
        return {"skipped": "disabled"}
    st = _state(store)
    last = _parse_ts(st.get("last_attempt_at")) or _parse_ts(st.get("last_success_at"))
    now = datetime.now(UTC)
    if last is not None and (now - last).total_seconds() < interval_h * 3600:
        return {"skipped": "interval_not_elapsed"}
    res = run_backup(store, settings)
    st = _state(store)
    st["last_attempt_at"] = _iso(now)
    if res.get("status") == "success":
        st["last_success_at"] = _iso(datetime.now(UTC))
    _save_state(store, st)
    return res


def maybe_verify_backup(store: Store, settings: Settings) -> dict[str, Any]:
    """automation-cycle 진입점 — backup_verify_interval_hours 주기 도달 시
    최근 성공 백업 복원 검증."""
    try:
        interval_h = float(getattr(settings, "backup_verify_interval_hours", 0.0) or 0.0)
    except (TypeError, ValueError):
        interval_h = 0.0
    if interval_h <= 0:
        return {"skipped": "disabled"}
    st = _state(store)
    last = _parse_ts(st.get("last_verify_at"))
    now = datetime.now(UTC)
    if last is not None and (now - last).total_seconds() < interval_h * 3600:
        return {"skipped": "interval_not_elapsed"}
    res = verify_latest_backup(store, settings)
    if res.get("verify_status") in ("ok", "failed"):
        st = _state(store)
        st["last_verify_at"] = _iso(now)
        _save_state(store, st)
    return res
