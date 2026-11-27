import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** 헤더 푸시 토글 — 레거시 #push-toggle 이식 (PushSection과 동일 플로우). */
export function PushButton() {
  const { t } = useI18n();
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let dead = false;
    (async () => {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        if (!dead) setOn(false);
        return;
      }
      try {
        const reg = await navigator.serviceWorker.getRegistration("/");
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (!dead) setOn(!!sub);
      } catch { if (!dead) setOn(false); }
    })();
    return () => { dead = true; };
  }, []);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (on) {
        const reg = await navigator.serviceWorker.getRegistration("/");
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (sub) {
          try { await apiPost("/push/unsubscribe", { endpoint: sub.endpoint }); }
          finally { await sub.unsubscribe(); }
        }
        setOn(false);
      } else {
        const reg = await navigator.serviceWorker.register("/sw.js");
        const { public_key } = await apiGet<{ public_key?: string }>("/push/vapid-key");
        if (!public_key) { alert(t("hdr.pushNoKey", "서버에 VAPID 키가 없습니다")); return; }
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(public_key),
        });
        await apiPost("/push/subscribe", sub.toJSON());
        setOn(true);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      className={`rounded-md p-2 transition-colors hover:bg-surface-hover ${on ? "text-accent" : "text-muted"}`}
      onClick={toggle}
      disabled={busy}
      title={t("hdr.push", "알림")}
      aria-label={t("hdr.push", "알림")}
    >
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
    </button>
  );
}
