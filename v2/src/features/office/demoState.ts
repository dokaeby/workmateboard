/** office 데모 모드 공유 상태 — 레거시 localStorage 'jobmate.officeDemo' 이식.
 * 보드(StageSection)와 스프라이트(OfficeSpriteSection)가 같은 상태를
 * 구독한다 — 한쪽에서 토글하면 다른쪽도 즉시 반영된다. */
import { useSyncExternalStore } from "react";

const KEY = "jobmate.officeDemo";
let value = read();
const listeners = new Set<() => void>();

function read(): boolean {
  try { return localStorage.getItem(KEY) !== "0"; } catch { return true; }
}

export function officeDemoEnabled(): boolean { return value; }

export function setOfficeDemo(on: boolean) {
  value = on;
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch { /* noop */ }
  listeners.forEach((f) => f());
}

function subscribe(f: () => void) {
  listeners.add(f);
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY) { value = read(); f(); }
  };
  window.addEventListener("storage", onStorage);
  return () => { listeners.delete(f); window.removeEventListener("storage", onStorage); };
}

export function useOfficeDemo(): [boolean, (on: boolean) => void] {
  const on = useSyncExternalStore(subscribe, () => value);
  return [on, setOfficeDemo];
}
