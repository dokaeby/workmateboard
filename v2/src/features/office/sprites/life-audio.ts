/** 알림 소리 — office-life.ts 에서 떼어낸 모듈.
 * 승인·질문이 새로 생기면 짧은 차임(WebAudio 진동자, 파일 없음) */

const MUTE_KEY = "jobmate.officeMute";
let muted = (() => { try { return localStorage.getItem(MUTE_KEY) === "1"; } catch { return false; } })();
let actx: AudioContext | null = null;
function audio(): AudioContext | null {
  if (!actx) {
    try {
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      actx = AC ? new AC() : null;
    } catch { actx = null; }
  }
  if (actx && actx.state === "suspended") actx.resume().catch(() => {});
  return actx;
}
export function chime() {
  if (muted) return;
  const ctx = audio();
  if (!ctx) return;
  const t0 = ctx.currentTime + 0.01;
  for (const [f, d, v] of [[880, 0, 0.12], [1318.5, 0.1, 0.1]] as [number, number, number][]) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + d);
    g.gain.exponentialRampToValueAtTime(v, t0 + d + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + d + 0.3);
    o.connect(g); g.connect(ctx.destination);
    o.start(t0 + d); o.stop(t0 + d + 0.32);
  }
}
// 첫 클릭 뒤 AudioContext 재개 — 브라우저 자동재생 정책 대비
export function wakeAudio() { audio(); }
export const soundMuted = () => muted;
export function setSoundMuted(v: boolean) {
  muted = !!v;
  try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch { /* 저장 못 해도 지금은 적용된다 */ }
}
