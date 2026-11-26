/** playbooks 뷰 i18n 사전. */

import { registerI18n } from "../../shared/i18n";

export const STR: { ko: Record<string, string>; en: Record<string, string> } = {
  ko: {
    "pb.title": "플레이북",
    "pb.desc": "자주 쓰는 워크플로 실행 템플릿",
    "pb.new": "새 플레이북",
    "pb.empty": "플레이북이 없습니다",
    "pb.unnamed": "(이름 없음)",
    "pb.namePh": "이름 (예: 주간 리뷰)",
    "pb.noWorkflow": "워크플로 없음",
    "pb.taskPh": "작업 내용 (task) — 플레이북 실행 시 이 입력이 사용됩니다",
    "pb.needAll": "이름·워크플로·작업 내용을 모두 입력하세요",
    "pb.run": "실행",
    "pb.running": "실행 중…",
    "pb.runStarted": "실행을 시작했습니다: {id}",
    "pb.confirmDelete": "'{name}' 플레이북을 삭제할까요?",
    "common.creating": "생성 중…",
    "common.created": "생성했습니다",
    "common.cancel": "취소",
    "common.create": "생성",
    "common.close": "닫기",
  },
  en: {
    "pb.title": "Playbooks",
    "pb.desc": "Reusable workflow run templates",
    "pb.new": "New playbook",
    "pb.empty": "No playbooks",
    "pb.unnamed": "(untitled)",
    "pb.namePh": "Name (e.g. Weekly review)",
    "pb.noWorkflow": "No workflows",
    "pb.taskPh": "Task input — used when the playbook runs",
    "pb.needAll": "Fill in name, workflow, and task",
    "pb.run": "Run",
    "pb.running": "Running…",
    "pb.runStarted": "Run started: {id}",
    "pb.confirmDelete": "Delete playbook '{name}'?",
    "common.creating": "Creating…",
    "common.created": "Created",
    "common.cancel": "Cancel",
    "common.create": "Create",
    "common.close": "Close",
  },
};

registerI18n(STR);
