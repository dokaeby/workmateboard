/** 푸시 알림 — Web Push 구독 상태 확인/켜기/끄기.
 *  레거시 index.html의 #push-toggle 플로우 이식:
 *  /push/vapid-key 조회 → PushManager.subscribe(applicationServerKey)
 *  → endpoint/keys를 /push/subscribe에 등록, 해제는 /push/unsubscribe.
 *  serviceWorker는 백엔드가 서빙하는 /sw.js 를 사용한다. */
import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../../../shared/api/client";
import { useI18n } from "../../../shared/i18n";
import { Card, Button, Badge } from "../../../shared/ui";

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window;
}

/** iOS/iPadOS Safari는 홈 화면에 추가된 PWA에서만 Web Push 가능 (iOS 16.4+). */
function iosNeedsStandalone(): boolean {
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches
    || (navigator as { standalone?: boolean }).standalone === true;
  return isIOS && !isStandalone;
}

export default function PushSection() {
  const { t } = useI18n();
  // 미지원 브라우저는 확인 없이 바로 "꺼짐" — 지원 시 null(확인 중)로 시작해 effect에서 조회
  const [subscribed, setSubscribed] = useState<boolean | null>(() =>
    pushSupported() ? null : false,
  );
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const reg = await navigator.serviceWorker.getRegistration("/");
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      setSubscribed(!!sub);
    } catch {
      setSubscribed(false);
    }
  }, []);

  useEffect(() => {
    if (!pushSupported()) return;
    // effect 본문 동기 setState 방지 — 다음 틱에서 비동기 조회 시작
    const id = setTimeout(() => { void refresh(); }, 0);
    return () => clearTimeout(id);
  }, [refresh]);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (subscribed) {
        const reg = await navigator.serviceWorker.getRegistration("/");
        const sub = reg ? await reg.pushManager.getSubscription() : null;
        if (sub) {
          try {
            await apiPost("/push/unsubscribe", { endpoint: sub.endpoint });
          } finally {
            await sub.unsubscribe();
          }
        }
        setSubscribed(false);
      } else {
        if (iosNeedsStandalone()) {
          alert(t("settings.push.iosNeed",
            "아이폰/아이패드에서는 Safari의 '공유 → 홈 화면에 추가'로 jobMate를 설치한 뒤,\n설치된 앱에서 알림을 켜주세요. (iOS 16.4 이상)"));
          return;
        }
        if (!pushSupported()) {
          alert(t("settings.push.unsupported",
            "이 브라우저는 Web Push를 지원하지 않습니다.\nSafari는 macOS 13+ / iOS 16.4+에서 지원됩니다."));
          return;
        }
        const reg = await navigator.serviceWorker.register("/sw.js");
        const { public_key } = await apiGet<{ public_key?: string }>("/push/vapid-key");
        if (!public_key) {
          alert(t("settings.push.noKey", "서버에 VAPID_PUBLIC_KEY가 설정되지 않았습니다."));
          return;
        }
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(public_key),
        });
        await apiPost("/push/subscribe", sub.toJSON());
        setSubscribed(true);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const supported = pushSupported();
  return (
    <Card title={t("settings.sec.push", "푸시 알림")} className="mb-4">
      <p className="mb-3 text-sm text-muted">
        {t("settings.sec.push.desc", "이 기기에서 세션 알림 푸시를 받습니다. 끄면 이 브라우저의 구독만 해제됩니다.")}
      </p>
      <div className="flex items-center gap-3">
        {subscribed === null ? (
          <span className="text-sm text-muted">{t("settings.push.checking", "확인 중…")}</span>
        ) : (
          <>
            <Badge tone={subscribed ? "ok" : "muted"}>
              {subscribed
                ? t("settings.push.on", "켜짐")
                : t("settings.push.off", "꺼짐")}
            </Badge>
            <Button
              variant={subscribed ? "secondary" : "primary"}
              className="px-3 py-1.5 text-xs"
              disabled={busy || (!supported && !subscribed)}
              onClick={toggle}
            >
              {busy
                ? t("settings.saving", "저장 중…")
                : subscribed
                  ? t("settings.push.turnOff", "알림 끄기")
                  : t("settings.push.turnOn", "알림 켜기")}
            </Button>
          </>
        )}
      </div>
      {!supported && subscribed === false && (
        <p className="mt-2 text-xs text-muted">
          {t("settings.push.unsupportedHint",
            "이 브라우저는 Web Push를 지원하지 않습니다. Safari는 macOS 13+ / iOS 16.4+에서 지원됩니다.")}
        </p>
      )}
    </Card>
  );
}
