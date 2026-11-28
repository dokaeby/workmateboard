"""템플릿 마켓플레이스 엔진 — export/import 번들 + 시크릿 스캔 (spec 006 US3).

포맷 계약 (TemplateBundle, FR-012):
    {format_version: "1.0", kind: workflow|playbook|bundle, name,
     spec?, playbook?, playbooks?, exported_at, checksum, warnings?}

- checksum은 {format_version, kind, name, spec|playbook|playbooks|refs}
  내용 필드의 canonical JSON SHA-256 — exported_at/warnings/checksum 자체는
  제외해 export 시각과 무관하게 결정적이다(SC-005 byte-equivalent 왕복).
- api_key_env는 환경변수 "이름"만 담는다 — 값은 스키마상 존재하지 않고,
  import 시 env-name 형식이 아니면 필드 에러로 거부한다.
- 평문 시크릿 패턴 스캔(FR-013): export는 발견 시 warnings에 기록하고
  허용, import는 secret_detected 422로 거부하고 위치(path+pattern)를 보고.
- 이름 충돌 정책(문서화된 결정 — spec의 "{name}-imported 또는 409" 중
  suffix 선택): custom_workflows에 동명 행이 있으면 `{name}-imported`,
  그래도 있으면 `-imported-2`, `-imported-3`…으로 유일 이름을 만든다.
  spec.key도 최종 등록 이름으로 재작성해 builder의 key==name 불변식을 유지.
  playbook도 동명 playbook이 있으면 동일 규칙으로 suffix를 붙인다.
- builtin 이름(_BUILTIN_WF_KEYS)은 export 시 warnings에 기록하고,
  import 시에는 422로 거부한다(사용자 정의가 내장 spec을 가리는 것을 방지).
- ALL-OR-NOTHING(FR-014): 모든 페이로드를 쓰기 전에 검증하고, 쓰기 도중
  실패하면 생성분을 역순 보상 삭제한다(rollback). store.tx()는 중첩
  커밋을 지원하지 않으므로 보상 삭제 방식 — 관측 가능한 부분 등록은 없다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ..models import WorkflowSpec
from ..workflows.resolver import resolve_workflow_spec

logger = logging.getLogger("jobmate.templates")

FORMAT_VERSION = "1.0"
_KINDS = ("workflow", "playbook", "bundle")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 평문 시크릿 패턴 — 매칭된 "값"은 절대 로그/응답에 포함하지 않고
# 위치(path)와 패턴 이름만 보고한다.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("slack_webhook_url", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_secret", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
)

# 이 이름의 문자열 필드는 "평문 시크릿"으로 간주 — api_key_env(이름만 허용)와
# 달리 값 자체가 비밀이어야 하는 필드다. WorkflowSpec에는 없는 키라 extra
# 필드·playbook input 등 임의 위치에서도 잡아낸다.
_PLAINTEXT_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "secret", "password", "passwd", "access_token", "private_key"}
)

# checksum/시각/경고는 콘텐츠가 아니므로 스캔·체크섬 대상에서 제외.
_META_KEYS = frozenset({"format_version", "kind", "checksum", "exported_at", "warnings"})
_CHECKSUM_KEYS = ("format_version", "kind", "name", "spec", "playbook", "playbooks", "refs")


class TemplateError(Exception):
    """템플릿 export/import 실패 — 라우터가 status/code/details로 err()에 매핑."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_checksum(bundle: dict[str, Any]) -> str:
    """콘텐츠 필드 canonical JSON의 SHA-256 — 결정적(SC-005)."""
    content = {k: bundle[k] for k in _CHECKSUM_KEYS if k in bundle}
    return "sha256:" + hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def verify_checksum(bundle: dict[str, Any]) -> bool:
    expected = str(bundle.get("checksum") or "")
    if not expected:
        return True  # checksum 없는 외부 작성 번들도 허용(스키마 검증이 방어선)
    return expected == compute_checksum(bundle)


def scan_secrets(node: Any, path: str = "$") -> list[dict[str, str]]:
    """JSON 트리 재귀 스캔 — {"path": "$.spec.stages[0].x", "pattern": name}.

    문자열 "값"만 검사한다(키 이름이 아니라 값 기준 — 프롬프트 텍스트에
    우연히 패턴이 들어가도 잡되, 위치를 보고해 사람이 판단하게 한다).
    평문 시크릿 키(_PLAINTEXT_SECRET_KEYS)는 값과 무관하게 pattern
    "sensitive_field"로 보고한다."""
    findings: list[dict[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}"
            if isinstance(k, str) and k.lower() in _PLAINTEXT_SECRET_KEYS and isinstance(v, str):
                if v.strip():
                    findings.append({"path": child, "pattern": "sensitive_field"})
            findings.extend(scan_secrets(v, child))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            findings.extend(scan_secrets(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        for name, pat in SECRET_PATTERNS:
            if pat.search(node):
                findings.append({"path": path, "pattern": name})
                break  # 한 위치당 첫 매칭 패턴만 — 중복 보고 방지
    return findings


def _spec_dict_for_export(store: Any, name: str) -> tuple[dict[str, Any] | None, bool]:
    """workflow export용 spec dict + is_builtin 반환.

    builtin은 WORKFLOWS spec을 dump. 커스텀은 최신 workflow_revisions spec을
    우선 사용하고(FR-012), 리비전 행이 없으면 custom_workflows.spec으로 폴백.
    반환 spec은 WorkflowSpec.model_validate를 통과한 정규화 dict다."""
    resolved = resolve_workflow_spec(store, name)
    if resolved is None:
        return None, False
    if name in _builtin_keys():
        return resolved.model_dump(mode="json"), True
    row = store.get_custom_workflow(name)
    if row is None:
        return None, False
    latest = int(row.get("latest_revision") or 0)
    raw: Any = None
    if latest > 0:
        rev = store.get_custom_workflow_revision(name, latest)
        if rev is not None:
            raw = rev.get("spec")
    if raw is None:
        raw = row.get("spec")
    try:
        spec = WorkflowSpec.model_validate(raw)
    except Exception as e:
        raise TemplateError(
            422, "invalid_spec", f"stored workflow spec failed validation: {str(e)[:200]}"
        ) from e
    return spec.model_dump(mode="json"), False


def _builtin_keys() -> frozenset[str]:
    from ..workflows import WORKFLOWS

    return frozenset(WORKFLOWS)


def _warnings_for(
    bundle_content: dict[str, Any], *, builtin_collision: bool
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if builtin_collision:
        warnings.append(
            {
                "code": "builtin_name",
                "message": "name collides with a builtin workflow key",
            }
        )
    findings = scan_secrets(bundle_content)
    if findings:
        warnings.append(
            {
                "code": "secret_patterns",
                "message": "plaintext secret-like patterns detected — review before sharing",
                "findings": findings,
            }
        )
    return warnings


def export_template(
    store: Any,
    *,
    kind: str,
    ref_id: str,
    builtin_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """{kind, ref_id} → TemplateBundle dict. 실패 시 TemplateError."""
    if kind not in _KINDS:
        raise TemplateError(422, "validation", f"kind must be one of {list(_KINDS)}")
    ref = str(ref_id or "").strip()
    if not ref:
        raise TemplateError(422, "validation", "ref_id is required")
    bk = builtin_keys if builtin_keys is not None else _builtin_keys()

    bundle: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "exported_at": _now_iso(),
    }
    content: dict[str, Any] = {}
    builtin_collision = False

    if kind in ("workflow", "bundle"):
        name = ref
        spec_dict, is_builtin = _spec_dict_for_export(store, name)
        if spec_dict is None:
            raise TemplateError(404, "not_found", f"workflow '{name}' not found")
        # spec.key와 등록명이 어긋난 비정상 행은 export하지 않는다 —
        # import 측의 key==name 불변식과 충돌해 왕복이 깨진다.
        if str(spec_dict.get("key") or "") != name:
            raise TemplateError(
                422, "invalid_spec", "workflow spec.key does not match its registered name"
            )
        bundle["name"] = name
        bundle["spec"] = spec_dict
        bundle["source"] = "builtin" if is_builtin else "custom"
        content["spec"] = spec_dict
        # builtin 이름과 충돌하는 "커스텀" 행이 실제로 존재할 때만 경고 —
        # builtin spec 자체를 export하는 정상 경우는 경고하지 않는다.
        builtin_collision = name in bk and store.get_custom_workflow(name) is not None
        if kind == "bundle":
            pbs = [
                {
                    "name": p.get("name"),
                    "workflow": p.get("workflow"),
                    "input": p.get("input") or {},
                }
                for p in store.list_playbooks()
                if str(p.get("workflow") or "") == name
            ]
            bundle["playbooks"] = pbs
            content["playbooks"] = pbs
    else:  # playbook
        pb = store.get_playbook(ref)
        if pb is None:
            raise TemplateError(404, "not_found", "playbook not found")
        wf_name = str(pb.get("workflow") or "")
        playbook = {
            "name": pb.get("name"),
            "workflow": wf_name,
            "input": pb.get("input") or {},
        }
        bundle["name"] = str(pb.get("name") or ref)
        bundle["playbook"] = playbook
        content["playbook"] = playbook
        # 참조 워크플로가 커스텀이면 spec도 함께 실어 자기완결 번들로 만든다.
        # builtin 참조는 refs만 기록 — 대상 인스턴스에 이미 내장이기 때문.
        if wf_name:
            spec_dict, is_builtin = _spec_dict_for_export(store, wf_name)
            if spec_dict is not None and not is_builtin:
                bundle["spec"] = spec_dict
                content["spec"] = spec_dict
                bundle["refs"] = {"workflow": wf_name}
                content["refs"] = {"workflow": wf_name}
            elif spec_dict is not None:
                bundle["refs"] = {"workflow": wf_name}
                content["refs"] = {"workflow": wf_name}

    warnings = _warnings_for(content, builtin_collision=builtin_collision)
    if warnings:
        bundle["warnings"] = warnings
    bundle["checksum"] = compute_checksum(bundle)
    return bundle


def _field_errors_from_pydantic(exc: Exception) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for e in getattr(exc, "errors", lambda: [])():
        loc = ".".join(str(p) for p in e.get("loc", ())) or "(root)"
        errors.append({"field": loc, "message": str(e.get("msg") or "")[:200]})
    return errors or [{"field": "(root)", "message": str(exc)[:200]}]


def _validate_playbook_entry(pb: Any, idx: int) -> dict[str, Any]:
    """playbook/playbooks[] 항목 정규화 — 실패 시 TemplateError를 즉시 던진다."""
    if not isinstance(pb, dict):
        raise TemplateError(422, "invalid_playbook", f"playbooks[{idx}] must be an object")
    name = str(pb.get("name") or "").strip()
    if not name or len(name) > 120:
        raise TemplateError(422, "invalid_playbook", f"playbooks[{idx}].name must be 1-120 chars")
    wf = str(pb.get("workflow") or "").strip()
    if not wf or len(wf) > 120:
        raise TemplateError(
            422, "invalid_playbook", f"playbooks[{idx}].workflow must be 1-120 chars"
        )
    inp = pb.get("input") or {}
    if not isinstance(inp, dict):
        raise TemplateError(422, "invalid_playbook", f"playbooks[{idx}].input must be an object")
    return {"name": name, "workflow": wf, "input": inp}


def _unique_name(name: str, taken: set[str]) -> tuple[str, bool]:
    """충돌 시 `{name}-imported[-N]` suffix — 문서화된 충돌 정책."""
    if name not in taken:
        return name, False
    base = f"{name}-imported"
    candidate, i = base, 2
    while candidate in taken:
        candidate = f"{base}-{i}"
        i += 1
    return candidate, True


def import_template(
    store: Any,
    payload: Any,
    *,
    builtin_keys: set[str] | frozenset[str] | None = None,
    created_by: str = "",
) -> dict[str, Any]:
    """TemplateBundle dict → 신규 등록. 실패 시 TemplateError, 성공 시
    {"imported": [...], "renamed": {...}}. 부분 등록 없음(보상 롤백)."""
    if not isinstance(payload, dict):
        raise TemplateError(422, "validation", "import body must be a JSON object")
    fv = str(payload.get("format_version") or "")
    if fv != FORMAT_VERSION:
        raise TemplateError(
            422,
            "unsupported_format_version",
            f"unsupported format_version '{fv}' (expected '{FORMAT_VERSION}')",
            details={"format_version": fv},
        )
    kind = str(payload.get("kind") or "")
    if kind not in _KINDS:
        raise TemplateError(422, "validation", f"kind must be one of {list(_KINDS)}")
    bk = builtin_keys if builtin_keys is not None else _builtin_keys()

    name = str(payload.get("name") or "").strip()
    # 번들 name은 템플릿 표시 이름 — kind=playbook은 플레이북 이름(자유 형식)이라
    # 워크플로 키 패턴을 강제하지 않는다. 키 패턴은 아래 spec.key 검사에서 강제.
    if not name or len(name) > 200:
        raise TemplateError(
            422,
            "validation",
            "name must be 1-200 chars",
            details={"errors": [{"field": "name", "message": "name is required"}]},
        )

    if not verify_checksum(payload):
        raise TemplateError(422, "checksum_mismatch", "bundle checksum does not match content")

    # 시크릿 스캔 — 메타 키 제외 전체 콘텐츠. 발견 시 위치와 함께 거부.
    scan_target = {k: v for k, v in payload.items() if k not in _META_KEYS}
    findings = scan_secrets(scan_target)
    if findings:
        raise TemplateError(
            422,
            "secret_detected",
            "payload contains plaintext secret-like patterns",
            details={"findings": findings},
        )

    # ---- 1) 스키마 검증 (쓰기 전 전부) ----
    spec_raw = payload.get("spec")
    spec_obj: WorkflowSpec | None = None
    if spec_raw is not None:
        if not isinstance(spec_raw, dict):
            raise TemplateError(
                422,
                "invalid_spec",
                "spec must be an object",
                details={"errors": [{"field": "spec", "message": "must be an object"}]},
            )
        try:
            spec_obj = WorkflowSpec.model_validate(spec_raw)
        except Exception as e:
            raise TemplateError(
                422,
                "invalid_spec",
                "spec is not a valid WorkflowSpec",
                details={"errors": _field_errors_from_pydantic(e)},
            ) from e
        if spec_obj.key != name:
            # kind=playbook 번들은 name이 playbook 이름이고 spec.key는
            # refs.workflow(워크플로 이름)과 일치해야 한다.
            expected = name
            if kind == "playbook":
                refs = payload.get("refs")
                if isinstance(refs, dict) and refs.get("workflow"):
                    expected = str(refs["workflow"])
            if spec_obj.key != expected:
                raise TemplateError(
                    422,
                    "invalid_spec",
                    "spec.key must equal the bundle name (or refs.workflow for playbook bundles)",
                    details={
                        "errors": [
                            {
                                "field": "spec.key",
                                "message": f"'{spec_obj.key}' != '{expected}'",
                            }
                        ]
                    },
                )
        # 등록될 워크플로 키(spec.key)는 커스텀 워크플로 API와 동일한
        # 키 패턴을 강제 — suffix는 패턴을 보존한다.
        if not _NAME_RE.match(spec_obj.key):
            raise TemplateError(
                422,
                "invalid_spec",
                "spec.key must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
                details={
                    "errors": [{"field": "spec.key", "message": "invalid workflow key format"}]
                },
            )
        # playbook 번들에 실린 워크플로 spec의 key도 builtin 이름이면 거부 —
        # 번들 name(플레이북 이름)만 검사하면 builtin 워크플로가 새어 들어온다.
        if spec_obj.key in bk:
            raise TemplateError(
                422,
                "builtin_name",
                f"'{spec_obj.key}' is a builtin workflow name and cannot be imported",
                details={"errors": [{"field": "spec.key", "message": "builtin workflow name"}]},
            )
        # api_key_env는 env "이름"만 — 리터럴 키/값 형태는 거부.
        bad_env = _bad_api_key_env_values(spec_obj.model_dump(mode="json"))
        if bad_env:
            raise TemplateError(
                422,
                "invalid_spec",
                "api_key_env must contain an environment variable name, not a secret value",
                details={"errors": bad_env},
            )

    # playbook 항목 수집 — kind에 따라 단수/복수 필드.
    playbook_entries: list[dict[str, Any]] = []
    pb_raw = payload.get("playbook")
    if pb_raw is not None:
        playbook_entries.append(_validate_playbook_entry(pb_raw, 0))
    pbs_raw = payload.get("playbooks")
    if pbs_raw is not None:
        if not isinstance(pbs_raw, list):
            raise TemplateError(422, "invalid_playbook", "playbooks must be an array")
        for i, pb in enumerate(pbs_raw):
            playbook_entries.append(_validate_playbook_entry(pb, i))

    # kind별 최소 요구 — workflow/bundle은 spec, playbook은 playbook 필수.
    if kind in ("workflow", "bundle") and spec_obj is None:
        raise TemplateError(
            422,
            "validation",
            f"kind='{kind}' requires a 'spec' field",
            details={"errors": [{"field": "spec", "message": "required"}]},
        )
    if kind == "playbook" and not playbook_entries:
        raise TemplateError(
            422,
            "validation",
            "kind='playbook' requires a 'playbook' field",
            details={"errors": [{"field": "playbook", "message": "required"}]},
        )

    # ---- 2) 이름 충돌 해석 + playbook workflow 참조 사전 검증 ----
    # workflow 등록 기준 이름은 spec.key — kind=playbook 번들은 번들 name이
    # 플레이북 이름이므로 spec.key가 실제 워크플로 식별자다.
    spec_key = spec_obj.key if spec_obj is not None else ""
    taken_wf = {str(r.get("name")) for r in store.list_custom_workflows()}
    renamed: dict[str, str] = {}
    if spec_obj is not None:
        final_wf_name, did_rename = _unique_name(spec_key, taken_wf)
        if did_rename:
            renamed["workflow"] = f"{spec_key} -> {final_wf_name}"
    else:
        final_wf_name = ""

    # playbook 이름 충돌 — 동명 playbook 존재 시 suffix(같은 규칙).
    existing_pb_names = {str(p.get("name")) for p in store.list_playbooks()}
    pb_plans: list[dict[str, Any]] = []
    for entry in playbook_entries:
        wf_ref = entry["workflow"]
        if spec_key and wf_ref == spec_key:
            # 번들에 실린 워크플로 참조 → 최종(개명 후) 이름으로 재작성.
            # 등록 전이라 resolve_workflow_spec로는 아직 해석되지 않으므로
            # 아래 해석 검사에서 final_wf_name은 면제한다.
            wf_ref = final_wf_name
        if wf_ref != final_wf_name and resolve_workflow_spec(store, wf_ref) is None:
            raise TemplateError(
                422,
                "unknown_workflow",
                f"playbook '{entry['name']}' references unresolvable workflow '{wf_ref}'",
                details={
                    "errors": [
                        {
                            "field": "playbook.workflow",
                            "message": f"workflow '{wf_ref}' is not registered",
                        }
                    ]
                },
            )
        pname, pb_renamed = _unique_name(entry["name"], existing_pb_names)
        if pb_renamed:
            renamed[f"playbook:{entry['name']}"] = f"{entry['name']} -> {pname}"
        existing_pb_names.add(pname)
        pb_plans.append({**entry, "workflow": wf_ref, "final_name": pname})

    # ---- 3) 등록 — workflow 먼저, playbook 나중. 실패 시 보상 롤백 ----
    created: list[tuple[str, str]] = []
    try:
        imported: list[dict[str, Any]] = []
        if spec_obj is not None:
            spec_dict = spec_obj.model_dump(mode="json")
            spec_dict["key"] = final_wf_name
            res = store.save_custom_workflow(
                final_wf_name,
                spec_dict,
                note=f"template import (from '{name}')",
                created_by=created_by,
            )
            if not res.get("ok"):
                raise TemplateError(
                    409 if res.get("conflict") else 500,
                    "revision_conflict" if res.get("conflict") else "save_failed",
                    "workflow registration failed",
                )
            created.append(("workflow", final_wf_name))
            item: dict[str, Any] = {
                "kind": "workflow",
                "name": final_wf_name,
                "revision": int(res.get("revision") or 0),
            }
            if final_wf_name != spec_key:
                item["renamed_from"] = spec_key
            imported.append(item)
        for plan in pb_plans:
            pb = store.create_playbook(
                plan["final_name"], plan["workflow"], plan["input"], created_by
            )
            pid = str(pb.get("id") or "")
            if pid:
                created.append(("playbook", pid))
            item = {"kind": "playbook", "name": plan["final_name"], "id": pid}
            if plan["final_name"] != plan["name"]:
                item["renamed_from"] = plan["name"]
            imported.append(item)
    except Exception:
        # ALL-OR-NOTHING — 생성분을 역순으로 제거한다.
        for ckind, cid in reversed(created):
            try:
                if ckind == "workflow":
                    store.delete_custom_workflow(cid)
                else:
                    store.delete_playbook(cid)
            except Exception:
                logger.debug("template import rollback failed kind=%s id=%s", ckind, cid)
        raise

    out: dict[str, Any] = {"ok": True, "imported": imported}
    if renamed:
        out["renamed"] = renamed
    return out


def _bad_api_key_env_values(node: Any, path: str = "spec") -> list[dict[str, str]]:
    """spec 트리의 api_key_env 값이 env 이름 형식인지 검사 — 리터럴 비밀값 거부."""
    bad: list[dict[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}"
            if k == "api_key_env" and isinstance(v, str) and v.strip():
                if not _ENV_NAME_RE.match(v.strip()):
                    bad.append(
                        {
                            "field": child,
                            "message": "api_key_env must be an environment variable name",
                        }
                    )
            bad.extend(_bad_api_key_env_values(v, child))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            bad.extend(_bad_api_key_env_values(v, f"{path}[{i}]"))
    return bad
