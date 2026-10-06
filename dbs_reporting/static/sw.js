// David's service worker: shows reminders as Windows pop-ups when they arrive by push, even when no David
// tab is open. Done and Snooze work right from the pop-up; clicking it opens To Do.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

const pages = () => self.clients.matchAll({ type: "window", includeUncontrolled: true });

self.addEventListener("push", (e) => {
  let r = {};
  try { r = e.data ? e.data.json() : {}; } catch { /* not ours */ }
  if (!r.id) return;
  e.waitUntil(Promise.all([
    self.registration.showNotification("Reminder from David", {
      body: r.title + (r.ticket ? ` (#${r.ticket})` : ""),
      tag: `todo-${r.id}`, renotify: true, requireInteraction: true, data: r,
      actions: [{ action: "done", title: "Done" }, { action: "snooze", title: "Snooze 15 min" }],
    }),
    pages().then((list) => list.forEach((c) => c.postMessage({ reminder: r }))),  // an open page shows its card too
  ]));
});

async function call(path, method, body) {
  const res = await fetch(path, { method, credentials: "same-origin", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(body) });
  for (const c of await pages()) c.postMessage({ refresh: true, item: body.item });
  return res;
}

self.addEventListener("notificationclick", (e) => {
  const r = e.notification.data || {};
  e.notification.close();
  if (e.action === "done") { e.waitUntil(call("/api/todo/done", "POST", { item: r.id, done: true })); return; }
  if (e.action === "snooze") {
    e.waitUntil(call("/api/todo/reminder", "PUT", { item: r.id, at: new Date(Date.now() + 15 * 60000).toISOString() }));
    return;
  }
  e.waitUntil(pages().then((list) => {
    const page = list.find((c) => new URL(c.url).origin === self.location.origin);
    if (page) { page.postMessage({ open: "todo" }); return page.focus(); }
    return self.clients.openWindow("/#todo");
  }));
});
