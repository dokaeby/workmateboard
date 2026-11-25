"""skillRadar 포팅 기능 테스트 — collect/dedupe/sanitize/classify/summarize/verify/rank/publish."""

from jobmate.adapters.fakes import FakeGitHub, FakeNotifier
from jobmate.models import RadarClassification
from jobmate.workflows.registry_builder import build_runner

from .helpers import make_env, scripted_llm


def _repo(name, stars=100, **kw):
    r = {
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": f"desc {name}",
        "stars": stars,
        "language": "Python",
        "topics": ["agent"],
        "archived": False,
        "fork": False,
        "license": "MIT",
    }
    r.update(kw)
    return r


def _summary_dict(i=0, supported=True):
    # verify_summary는 schema 없이 호출되므로 summarize와 같은 "" 큐를 공유한다.
    # "supported" 키를 함께 넣어 verify 판정이 가능하게 한다
    # (normalize_summary는 summary_* 키만 읽으므로 무시된다).
    return {
        "summary_en": [f"en {i} a", f"en {i} b", f"en {i} c"],
        "summary_zh": [f"zh {i} a", f"zh {i} b", f"zh {i} c"],
        "summary_ko": [f"ko 요약 {i}a", f"ko 요약 {i}b", f"ko 요약 {i}c"],
        "supported": supported,
    }


def _radar_llm(n=10, supported=True):
    """schema 이름별 큐: classify=RadarClassification, summarize/verify="" (dict)."""
    return {
        "RadarClassification": [
            RadarClassification(label="skills", confidence=0.9) for _ in range(n)
        ],
        "": [_summary_dict(i, supported=supported) for i in range(n)],
    }


def test_radar_full_flow():
    settings, store = make_env()
    gh = FakeGitHub(
        repos={"*": [_repo("a/x", 500), _repo("b/y", 300), _repo("c/z", 100, archived=True)]},
        readmes={"a/x": "# readme x", "b/y": "# readme y"},
    )
    notifier = FakeNotifier()
    adapters = {"github": gh, "notifier": notifier}
    llm = scripted_llm(_radar_llm(10))

    run = store.create_run("skill_radar", "0.2.0", {"task": "radar"}, trace_id="t")
    runner = build_runner("skill_radar", store=store, llm=llm, settings=settings, adapters=adapters)
    runner.invoke(run.id, {"task": "radar"}, gmail_ctx={"platform": "claude", "force": True})
    # skill_radar는 auto_approve ctx 없이도 workflow 단위로 자동 approve —
    # 어떤 생성 경로든 waiting_approval에 멈추지 않는다 (resume 불필요).

    dates = store.radar_dates("claude")
    assert dates, "no snapshot written"
    snap = store.radar_snapshot("claude", dates[0])
    items = store.radar_items(snap["id"])
    assert len(items) >= 1
    names = [i["repo_full_name"] for i in items]
    assert "c/z" not in names
    assert items[0]["rank"] == 1
    assert items[0]["summary_ko"].startswith("ko")
    assert items[0]["taxonomy"] == "skills"
    # skillRadar taxonomy 라벨 체계
    assert items[0]["summary_status"] == "ok"
    # 플랫폼 Top1 푸시 발송
    assert any(n["title"] == "Claude Top 1" for n in notifier.sent)


def test_radar_dedupe_only():
    settings, store = make_env()
    from jobmate.engine.functions import FunctionContext, radar_dedupe

    ctx = FunctionContext(
        state={
            "stage_outputs": {
                "collect": {
                    "repos": [
                        _repo("a/x"),
                        _repo("a/x"),
                        _repo("b/y", fork=True),
                        _repo("c/z", archived=True),
                    ]
                }
            }
        },
        store=store,
        settings=settings,
        adapters={},
        run_id="t",
    )
    out = radar_dedupe(ctx)
    names = [r["full_name"] for r in out["repos"]]
    assert names == ["a/x"]
    assert out["dropped"] == 3


def test_radar_publish_requires_approval():
    settings, store = make_env()
    from jobmate.engine.functions import FunctionContext, radar_publish

    ctx = FunctionContext(
        state={
            "approval": {"decision": "reject"},
            "stage_outputs": {"rank": {"repos": [], "date": "2026-01-01"}},
        },
        store=store,
        settings=settings,
        adapters={},
        run_id="t",
    )
    assert radar_publish(ctx)["status"] == "rejected"
