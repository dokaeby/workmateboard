// push payload의 url은 서버 생성값이지만 방어 심층으로 스킴/출처를 검증한다.
// same-origin(http/https) 또는 외부 https만 허용, 그 외(javascript:/data: 등)는 "/"로 폴백.
function safePushUrl(url) {
  try {
    const u = new URL(String(url || "/"), self.location.origin);
    if (u.origin === self.location.origin) return u.href;
    if (u.protocol === "https:") return u.href;
  } catch (e) { /* 파싱 실패 → 폴백 */ }
  return "/";
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "jobMate", body: event.data ? event.data.text() : "" };
  }
  const opts = {
    body: data.body || "",
    icon: "/static/icon.png",
    data: { url: safePushUrl(data.url) },
  };
  // 앱 아이콘 뱃지 — payload.badge(숫자)가 오면 표시
  if (data.badge != null && navigator.setAppBadge) {
    try { navigator.setAppBadge(Number(data.badge) || 0); } catch (e) {}
  }
  event.waitUntil(self.registration.showNotification(data.title || "jobMate", opts));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = safePushUrl(
    (event.notification.data && event.notification.data.url) || "/");
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) {
          w.navigate(url);
          return w.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
