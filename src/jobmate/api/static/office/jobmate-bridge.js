"use strict";
// jobMate 브리지 — index.html 인라인의 officeState / OFFICE_NODES(노드 배열)를
// 2D 사무실 사람 목록(Person, CONTRACT 4절)으로 변환해 window.JM_OFFICE_PROVIDER 에 건넨다.
// office-view.js 의 load() 가 3초마다 이 함수를 호출한다.
// doing/ask 는 평문 문자열 — I18N.act()/ask() 가 문자열을 그대로 통과시킨다.

const _jmBridgeProvider = async () => ({ people: jmOfficePeople() });
window.JM_OFFICE_PROVIDER = _jmBridgeProvider;
window.JM_BRIDGE_PROVIDER = _jmBridgeProvider;   // 세션 provider가 덮어써도 워크플로 provider를 되찾을 수 있게

// 감사 로그 마지막 항목을 한 줄로 — detail 은 객체일 수 있어 JSON으로 눌러 담는다
function jmAuditText(e) {
  if (!e) return "";
  const d = e.detail;
  const s = d == null ? "" : (typeof d === "string" ? d : JSON.stringify(d));
  return String(s || e.action || "").slice(0, 60);
}

function jmOfficePeople() {
  const run = officeState.run;
  if (!run) return [];                       // 실행 이력 없음 = 빈 사무실
  const audit = Array.isArray(run.audit) ? run.audit : [];
  const lastAudit = audit.length ? audit[audit.length - 1] : null;
  const runStatus = run.status || "";

  const people = OFFICE_NODES.map(n => {
    const p = { id: n.code, name: n.code + " " + n.label,
                status: "rest", ask: "", doing: "",
                subs: [], subCount: 0, kid: false, home: null, commits: null,
                log: [] };

    if (n.code === "ORCH") {
      // 오케스트레이터 = run 자체
      if (runStatus === "running" || runStatus === "queued") {
        p.status = "working"; p.doing = "워크플로 실행 중";
      } else if (runStatus === "waiting_approval" || runStatus === "waiting_review" ||
                 runStatus === "needs_human") {
        p.status = "decision"; p.ask = "run " + runStatus;
      } else {
        p.doing = "run " + (runStatus || "-");
      }
      // 활동 로그는 run 감사 기록 전체를 ORCH 가 담당한다
      p.log = audit.map(a => ({
        at: a.created_at, k: "text",
        a: [a.actor, a.action, jmAuditText(a)].filter(Boolean).join(" ").slice(0, 120),
      })).filter(e => e.at);
      return p;
    }

    const sr = n.key ? officeState.stages.get(n.key) : null;
    const st = sr ? sr.status : "pending";

    if (n.code === "HUM") {
      // 사람 승인 노드 — run이 승인 대기면 결정 대기, approval stage가 돌면 작업 중
      if (runStatus === "waiting_approval") {
        p.status = "decision"; p.ask = "사람 승인 대기";
      } else if (st === "running") {
        p.status = "working"; p.doing = jmAuditText(lastAudit) || "승인 처리 중";
      } else if (st === "succeeded") {
        p.doing = "승인 완료";
      } else if (st === "failed") {
        p.doing = "실패: " + String(sr.error || "").slice(0, 40);
      } else {
        p.doing = "대기";
      }
      return p;
    }

    // stage 노드
    if (st === "running") {
      p.status = "working";
      p.doing = jmAuditText(lastAudit) || n.label + " 진행 중";
    } else if (st === "succeeded") {
      p.doing = "완료";
    } else if (st === "failed") {
      const detail = sr.error || JSON.stringify(sr.output || "");
      p.doing = "실패: " + String(detail).slice(0, 40);
    } else if (st === "skipped") {
      p.doing = "건너뜀";
    } else {
      p.doing = "대기";
    }
    return p;
  });

  return people;
}
