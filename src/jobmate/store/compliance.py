# mypy: disable-error-code="attr-defined"
"""spec 005 US2 — 개인정보 처리 store 메서드.

- consent_records: 가입/재동의 이력(추가 전용 — UPDATE/DELETE는 계정 파기
  cascade에서만 사용자 단위로 수행).
- deletion_receipts: 계정 삭제 영수증 — user_id의 SHA-256 해시와 유형별
  처리 건수만 기록한다(이메일 등 PII 원문 금지, FR-016).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .util import _iso, _now


def _version_key(version: str) -> tuple[int, ...]:
    """'v1.2.3' → (1, 2, 3). 파싱 불가 세그먼트가 있으면 (0,) —
    비교 불가 버전은 "미동의"가 아니라 보수적으로 낮은 버전으로 취급하지
    않고, 호출 측에서 문자열 동등 비교로 폴백한다."""
    v = str(version or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts: list[int] = []
    for seg in v.split("."):
        if not seg.isdigit():
            return ()
        parts.append(int(seg))
    return tuple(parts) if parts else ()


def _version_lt(consented: str, required: str) -> bool:
    """consented 버전이 required보다 낮으면 True.

    둘 다 'vX.Y[.Z]' 형태면 수치 비교, 한쪽이라도 파싱 불가면 문자열
    동등 비교로 폴백(같지 않으면 낮은 것으로 간주해 재동의를 요구 —
    fail-safe 방향)."""
    ck, rk = _version_key(consented), _version_key(required)
    if ck and rk:
        n = max(len(ck), len(rk))
        ck += (0,) * (n - len(ck))
        rk += (0,) * (n - len(rk))
        return ck < rk
    return str(consented) != str(required)


class ComplianceStoreMixin:
    # ---- consent_records (FR-011/013) — 추가 전용 ----

    @staticmethod
    def user_id_hash(user_id: str) -> str:
        """삭제 영수증용 비가역 식별자 — SHA-256(user_id). 이메일·원문 id를
        영수증에 남기지 않기 위한 해시(FR-016)."""
        return hashlib.sha256(str(user_id).encode()).hexdigest()

    def record_consent(
        self,
        user_id: str,
        terms_version: str,
        privacy_version: str,
        ip: str = "",
        user_agent: str = "",
    ) -> str:
        """동의 이력 1행 추가 — 반환: consent_records.id.

        agreed_at과 created_at은 동일 시각(동의 행위 시점)."""
        cid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        with self.tx() as c:
            c.execute(
                "INSERT INTO consent_records(id,user_id,terms_version,privacy_version,"
                "agreed_at,ip,user_agent,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    cid,
                    user_id,
                    str(terms_version or ""),
                    str(privacy_version or ""),
                    now,
                    str(ip or "")[:64],
                    str(user_agent or "")[:300],
                    now,
                ),
            )
        return cid

    def latest_consent_for_user(self, user_id: str) -> dict[str, Any] | None:
        """사용자의 가장 최근 동의 행 — 재동의 판정의 기준."""
        r = self._conn.execute(
            "SELECT * FROM consent_records WHERE user_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(r) if r else None

    def list_consents_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """본인 데이터 export용 — 동의 이력 전체(최신순)."""
        rows = self._conn.execute(
            "SELECT id,terms_version,privacy_version,agreed_at,ip,user_agent,created_at"
            " FROM consent_records WHERE user_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT 1000",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def consent_missing_versions(
        self, user_id: str, terms_version: str, privacy_version: str
    ) -> list[str]:
        """재동의가 필요한 항목 라벨 목록 — 비어 있으면 최신 동의가 현행.

        반환 예: ["terms:v2.0", "privacy:v1.0"]. 동의 행이 아예 없으면
        (인증 도입 이전 계정 등) 두 항목 모두 반환한다 — enforce 모드에서
        처리는 호출 측(appctx)이 결정한다."""
        latest = self.latest_consent_for_user(user_id)
        missing: list[str] = []
        if latest is None or _version_lt(str(latest["terms_version"]), str(terms_version)):
            missing.append(f"terms:{terms_version}")
        if latest is None or _version_lt(str(latest["privacy_version"]), str(privacy_version)):
            missing.append(f"privacy:{privacy_version}")
        return missing

    # ---- deletion_receipts (FR-015/016) — 해시 식별자 + 건수만 ----

    def _deletion_receipt_insert(
        self,
        c: Any,
        user_id_hash: str,
        counts: dict[str, int],
        status: str = "completed",
    ) -> str:
        """호출 측 트랜잭션 커서에 영수증 INSERT — cascade와 같은 tx에 넣어
        삭제와 영수증 기록의 원자성을 확보한다. 반환: receipt id."""
        rid = uuid.uuid4().hex[:16]
        now = _iso(_now())
        c.execute(
            "INSERT INTO deletion_receipts(id,user_id_hash,requested_at,completed_at,"
            "counts_json,status) VALUES(?,?,?,?,?,?)",
            (
                rid,
                user_id_hash,
                now,
                now,
                json.dumps(counts or {}, ensure_ascii=False, sort_keys=True),
                status,
            ),
        )
        return rid

    def insert_deletion_receipt(
        self,
        user_id_hash: str,
        counts: dict[str, int],
        status: str = "completed",
    ) -> str:
        """단독 영수증 기록 — 반환: receipt id."""
        with self.tx() as c:
            return self._deletion_receipt_insert(c, user_id_hash, counts, status)

    def get_deletion_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM deletion_receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["counts"] = json.loads(d.get("counts_json") or "{}")
        except (TypeError, ValueError):
            d["counts"] = {}
        return d

    def latest_deletion_receipt_for_hash(self, user_id_hash: str) -> dict[str, Any] | None:
        """해당 사용자 해시의 가장 최근 영수증 — 삭제 직후 id 회수용."""
        r = self._conn.execute(
            "SELECT id FROM deletion_receipts WHERE user_id_hash=?"
            " ORDER BY completed_at DESC, id DESC LIMIT 1",
            (user_id_hash,),
        ).fetchone()
        return self.get_deletion_receipt(str(r["id"])) if r else None
