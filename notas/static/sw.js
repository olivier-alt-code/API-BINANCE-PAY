// Service worker: offline reading (cached pages) and offline writing (outbox + sync).
importScripts("/static/outbox.js");

const VERSION = "v7";
const STATIC = `notas-static-${VERSION}`;
const PAGES = "notas-pages";
const PRECACHE = [
  "/static/style.css", "/static/app.js", "/static/outbox.js", "/static/icon.svg",
  "/static/offline.html", "/manifest.webmanifest",
];
// Never cached nor queued: authentication and the password vault (decrypted secrets).
const PRIVATE = /^\/(entrar|configurar|salir|ajustes|claves|api\/)/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(STATIC).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("notas-static-") && key !== STATIC) await caches.delete(key);
    }
    if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
    await self.clients.claim();
  })());
});

const isLogin = (res) => res.redirected && new URL(res.url).pathname.startsWith("/entrar");

async function networkFirst(event) {
  const { request } = event;
  const cache = await caches.open(PAGES);
  try {
    const preload = await event.preloadResponse;
    const res = preload || await fetch(request);
    if (res.ok && !isLogin(res) && res.type === "basic") await cache.put(request, res.clone());
    return res;
  } catch (e) {
    const hit = await cache.match(request) || await cache.match(request, { ignoreSearch: true });
    return hit || caches.match("/static/offline.html");
  }
}

async function postOrQueue(request) {
  const copy = request.clone();
  try {
    return await fetch(request);
  } catch (e) {
    const fields = [...(await copy.formData()).entries()].filter(([, v]) => typeof v === "string");
    await Outbox.add(request.url, fields);
    try { await self.registration.sync.register("outbox"); } catch (_) { /* no Background Sync */ }
    const next = fields.find(([k]) => k === "next");
    const back = (next && next[1].startsWith("/") && !next[1].startsWith("//")) ? next[1] : (request.referrer || "/");
    const url = new URL(back, self.location.origin);
    url.hash = "en-cola";
    return Response.redirect(url.href, 303);
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.method === "POST") {
    if (PRIVATE.test(url.pathname)) return; // needs the server (login, vault)
    event.respondWith(postOrQueue(request));
    return;
  }
  if (request.method !== "GET") return;
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    // Stale-while-revalidate for assets.
    event.respondWith((async () => {
      const cache = await caches.open(STATIC);
      const hit = await cache.match(request, { ignoreSearch: true });
      const fresh = fetch(request).then((res) => { if (res.ok) cache.put(request, res.clone()); return res; }).catch(() => hit);
      return hit || fresh;
    })());
    return;
  }
  if (request.mode === "navigate") {
    if (PRIVATE.test(url.pathname)) {
      // Never stored offline; without connection show the offline page instead.
      event.respondWith(fetch(request).catch(() => caches.match("/static/offline.html")));
    } else {
      event.respondWith(networkFirst(event));
    }
  }
});

// One replay at a time (Background Sync and the page may both ask): no double posts.
let flushing = null;
function flushOnce() {
  if (!flushing) flushing = Outbox.flush().finally(() => { flushing = null; });
  return flushing;
}

async function syncOutbox() {
  const result = await flushOnce();
  const clients = await self.clients.matchAll({ type: "window" });
  clients.forEach((c) => c.postMessage({ type: "synced", ...result }));
  if (result.left && !result.needsLogin) throw new Error("retry later"); // Background Sync retries
  return result;
}

self.addEventListener("sync", (event) => {
  if (event.tag === "outbox") event.waitUntil(syncOutbox());
});

self.addEventListener("message", (event) => {
  const msg = event.data || {};
  if (msg.type === "flush") event.waitUntil(syncOutbox().catch(() => {}));
  if (msg.type === "clear-pages") event.waitUntil(caches.delete(PAGES));
  if (msg.type === "warm" && Array.isArray(msg.urls)) {
    // Pre-download pages so they can be read offline later: the links of the current
    // page plus every section/project/plan page the server lists.
    event.waitUntil((async () => {
      const cache = await caches.open(PAGES);
      let urls = msg.urls;
      try {
        const r = await fetch("/api/offline-urls", { credentials: "same-origin", cache: "no-store" });
        if (r.ok) urls = [...new Set([...(await r.json()).urls, ...urls])];
      } catch (e) { return; }
      for (const u of urls.slice(0, 150)) {
        const url = new URL(u, self.location.origin);
        if (url.origin !== self.location.origin || PRIVATE.test(url.pathname)) continue;
        try {
          const res = await fetch(url, { credentials: "same-origin" });
          if (res.ok && !isLogin(res)) await cache.put(url, res);
        } catch (e) { return; }
      }
    })());
  }
});
