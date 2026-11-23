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
    data: { url: data.url || "/" },
  };
  // 앱 아이콘 뱃지 — payload.badge(숫자)가 오면 표시
  if (data.badge != null && navigator.setAppBadge) {
    try { navigator.setAppBadge(Number(data.badge) || 0); } catch (e) {}
  }
  event.waitUntil(self.registration.showNotification(data.title || "jobMate", opts));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
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
