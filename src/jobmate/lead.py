"""팀장(lead) 대시보드 집계 — /projects, /risks, /approvals, /perf, 세션 회고.

insights.py와 같은 패턴: Store 읽기 + 순수 함수, 상태 변경 없음.
app.py의 create_app() 안에서 지연 import되어 순환 참조를 피한다.

구현은 형제 모듈로 분리되어 있고 이 모듈은 기존 공개 API를 그대로
re-export한다 (lead_common/lead_projects/lead_sessions/lead_risks/
lead_approvals/lead_perf).
"""

from .lead_approvals import approval_queue
from .lead_common import UNTAGGED, session_est_cost_usd, session_health, session_view
from .lead_perf import perf_compare
from .lead_projects import project_detail, project_overview
from .lead_risks import detect_risks
from .lead_sessions import session_history

__all__ = [
    "UNTAGGED",
    "approval_queue",
    "detect_risks",
    "perf_compare",
    "project_detail",
    "project_overview",
    "session_est_cost_usd",
    "session_health",
    "session_history",
    "session_view",
]
