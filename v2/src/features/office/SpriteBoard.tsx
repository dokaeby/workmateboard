/** SpriteBoard — 레거시 office-view.js 의 JMOffice2D start/stop + 캔버스 마운트를 React 로 이식.
 * canvas 마운트, ResizeObserver 대응, requestAnimationFrame 루프(30fps 스로틀),
 * people prop 이 바뀌면 life.sync() 로 들이고/보낸다. 프레임마다 life.update(dt) + view.render(). */

import { useEffect, useRef } from "react";
import { createLife, type Life } from "./sprites/office-life";
import { createOfficeView, type OfficeView } from "./sprites/office-view";
import type { LifeEvent, PersonInput } from "./sprites/types";
import * as I18N from "./sprites/office-i18n";
import { useI18n } from "../../shared/i18n";

const FRAME_MS = 1000 / 30;

export interface SpriteBoardProps {
  /** /agent-sessions → sessionsToPeople() 로 변환한 사람 목록 */
  people: PersonInput[];
  /** 고른 사람 id — 캔버스 고리·목록 하이라이트 공용 */
  selectedId?: string | null;
  /** 캐릭터/목록 클릭 — 같은 id 재클릭 시 null(해제) */
  onSelect?: (id: string | null) => void;
  /** 화면이 만든 사건(출근·퇴근·승인 대기 등) — 활동 로그 패널이 소비 */
  onEvents?: (events: LifeEvent[]) => void;
  /** 스프라이트 이미지 로드가 끝난 뒤 1회 호출 — failedImage 로 그림 로드 실패를 알린다 */
  onReady?: (info: { failedImage: boolean }) => void;
  /** 스프라이트 이미지 베이스 — 기본 "office" (v2/public/office) */
  assetBase?: string;
  /** 지도·통로 디버그 오버레이 */
  debug?: boolean;
  /** true면 시뮬레이션·렌더를 일시정지 (마운트는 유지 — 레거시 모드 토글 대응) */
  paused?: boolean;
  className?: string;
}

export function SpriteBoard({
  people, selectedId = null, onSelect, onEvents, onReady,
  assetBase, debug, paused = false, className,
}: SpriteBoardProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const lifeRef = useRef<Life | null>(null);
  const viewRef = useRef<OfficeView | null>(null);
  const cbRef = useRef({ onSelect, onEvents, onReady });
  const pausedRef = useRef(paused);
  const { lang } = useI18n();

  useEffect(() => {
    cbRef.current = { onSelect, onEvents, onReady };
  }, [onSelect, onEvents, onReady]);

  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  // ---------- 생명주기: life+view 생성, 이미지 로드, rAF 루프, ResizeObserver ----------
  useEffect(() => {
    const cv = canvasRef.current;
    const wrap = wrapRef.current;
    if (!cv || !wrap) return;

    const life = createLife();
    const view = createOfficeView(life, {
      assetBase,
      debug,
      onSelect: (id) => cbRef.current.onSelect?.(id),
    });
    lifeRef.current = life;
    viewRef.current = view;

    let raf = 0;
    let last = 0;
    let disposed = false;

    const frame = (now: number) => {
      if (disposed) { raf = 0; return; }
      raf = requestAnimationFrame(frame);
      if (document.hidden || pausedRef.current || now - last < FRAME_MS) return;
      const dt = Math.min(0.1, (now - (last || now)) / 1000);
      last = now;
      life.update(dt);
      const ctx = cv.getContext("2d");
      if (ctx) view.render(ctx);
      const evs = life.drainEvents();
      if (evs.length) cbRef.current.onEvents?.(evs);
    };

    const resize = () => view.resize(cv);
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    resize();

    view.loadImages().then(() => {
      if (disposed) return;
      resize();
      cbRef.current.onReady?.({ failedImage: view.failedImage });
    }).catch(() => {
      // loadImages 는 개별 onerror 를 삼키고 resolve 하지만, 예외 경로도 실패로 알린다
      if (!disposed) cbRef.current.onReady?.({ failedImage: true });
    });
    raf = requestAnimationFrame(frame);

    return () => {
      disposed = true;
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
      lifeRef.current = null;
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------- 데이터 동기화 — people 가 바뀌거나 언어가 바뀌면 life.sync ----------
  useEffect(() => {
    const life = lifeRef.current;
    if (!life) return;
    life.sync(people);
    const evs = life.drainEvents();
    if (evs.length) cbRef.current.onEvents?.(evs);
    // lang 변경 시 말풍선·이름표가 새 언어로 다시 만들어지게 재동기화(레거시 onChange 와 동일)
  }, [people, lang]);

  // ---------- 외부 선택 상태 → 캔버스 고리 ----------
  useEffect(() => {
    viewRef.current?.setSelected(selectedId);
  }, [selectedId]);

  // ---------- 클릭 — 캐릭터 hit-test → onSelect ----------
  const onClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const view = viewRef.current;
    const cv = canvasRef.current;
    if (!view || !cv) return;
    const r = cv.getBoundingClientRect();
    const id = view.hit(e.clientX - r.left, e.clientY - r.top);
    view.select(id);   // select() 가 토글 + onSelect 호출
  };
  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const view = viewRef.current;
    const cv = canvasRef.current;
    if (!view || !cv) return;
    const r = cv.getBoundingClientRect();
    cv.style.cursor = view.hit(e.clientX - r.left, e.clientY - r.top) ? "pointer" : "default";
  };

  // 첫 클릭 뒤 AudioContext 재개 — 브라우저 자동재생 정책 대비
  const wake = () => lifeRef.current?.wakeAudio();

  return (
    <div ref={wrapRef} className={className} style={{ width: "100%", height: "100%" }}>
      <canvas
        ref={canvasRef}
        onClick={onClick}
        onMouseMove={onMove}
        onPointerDown={wake}
        role="img"
        aria-label={I18N.t("canvas.label")}
        style={{ display: "block", width: "100%", height: "100%" }}
      />
    </div>
  );
}

export default SpriteBoard;
