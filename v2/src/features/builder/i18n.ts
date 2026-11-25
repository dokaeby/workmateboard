/** builder 뷰 i18n 사전 — 워크플로 빌더(커스텀 스펙 작성·저장·실행) 문자열. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "wf.title": "워크플로 빌더",
    "wf.desc": "커스텀 워크플로 스펙(JSON)을 작성·저장·실행",
    "wf.custom": "커스텀 워크플로",
    "wf.empty": "커스텀 워크플로가 없습니다",
    "wf.load": "불러오기",
    "wf.loaded": "불러왔습니다: {name}",
    "wf.confirmDelete": "'{name}' 워크플로를 삭제할까요?",
    "wf.needName": "워크플로 이름(key)을 입력하세요",
    "wf.needSpecJson": "스펙 JSON을 입력하세요",
    "wf.specNotObject": "스펙은 JSON 객체여야 합니다",
    "wf.jsonErr": "JSON 오류: {msg}",
    "wf.saved": "저장했습니다: {name}",
    "wf.runStarted": "실행 시작: {id}",
    "wf.namePh": "워크플로 이름 (key)",
    "wf.template": "템플릿 삽입",
    "wf.tpl.desc": "커스텀 워크플로 예시",
    "wf.tpl.analyze": "입력 분석",
    "wf.tpl.review": "결과 검수",
    "wf.tpl.done": "사람 확인",
    "wf.taskPh": "실행 시 task 입력 (선택)",
    "wf.specTitle": "스펙 형식",
    "wf.specFormat": `{
  "key": "my_flow",            // 워크플로 이름 — 저장 name과 반드시 일치
  "version": "0.1.0",
  "description": "...",
  "entry": "analyze",          // 시작 stage 키 (또는 "finalize")
  "stages": [
    {"key": "analyze", "kind": "llm",      "agent": "analysis", "description": "..."},
    {"key": "check",   "kind": "function", "function": "run_tests"},
    {"key": "done",    "kind": "human",    "description": "사람 확인"}
  ],
  "branches": {}               // stage_key -> {output_field: {value: next_stage}}
}`,
    "wf.specHint": "kind는 llm/function/human 중 하나. llm은 agent, function은 function 필수. spec.key는 워크플로 이름과 같아야 합니다.",
    "common.saving": "저장 중…",
    "common.save": "저장",
    "common.running": "실행 중…",
    "common.run": "실행",
    "common.delete": "삭제",
  },
  en: {
    "wf.title": "Workflow Builder",
    "wf.desc": "Write, save, and run custom workflow specs (JSON)",
    "wf.custom": "Custom workflows",
    "wf.empty": "No custom workflows",
    "wf.load": "Load",
    "wf.loaded": "Loaded: {name}",
    "wf.confirmDelete": "Delete workflow '{name}'?",
    "wf.needName": "Enter a workflow name (key)",
    "wf.needSpecJson": "Enter the spec JSON",
    "wf.specNotObject": "The spec must be a JSON object",
    "wf.jsonErr": "JSON error: {msg}",
    "wf.saved": "Saved: {name}",
    "wf.runStarted": "Run started: {id}",
    "wf.namePh": "Workflow name (key)",
    "wf.template": "Insert template",
    "wf.tpl.desc": "Custom workflow example",
    "wf.tpl.analyze": "Analyze input",
    "wf.tpl.review": "Review result",
    "wf.tpl.done": "Human review",
    "wf.taskPh": "Task input for the run (optional)",
    "wf.specTitle": "Spec format",
    "wf.specFormat": `{
  "key": "my_flow",            // workflow name — must match the saved name
  "version": "0.1.0",
  "description": "...",
  "entry": "analyze",          // starting stage key (or "finalize")
  "stages": [
    {"key": "analyze", "kind": "llm",      "agent": "analysis", "description": "..."},
    {"key": "check",   "kind": "function", "function": "run_tests"},
    {"key": "done",    "kind": "human",    "description": "human review"}
  ],
  "branches": {}               // stage_key -> {output_field: {value: next_stage}}
}`,
    "wf.specHint": "kind is one of llm/function/human. llm requires agent, function requires function. spec.key must equal the workflow name.",
    "common.saving": "Saving…",
    "common.save": "Save",
    "common.running": "Running…",
    "common.run": "Run",
    "common.delete": "Delete",
  },
};

registerI18n(STR);
