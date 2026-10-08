// Offline outbox shared by the page and the service worker (IndexedDB).
// Each item is a form POST that could not reach the server: {id, url, fields, label, at}.
(function (scope) {
  const DB = "notas-offline", STORE = "outbox";

  function open() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function tx(mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const result = fn(t.objectStore(STORE));
      t.oncomplete = () => resolve(result && "result" in result ? result.result : result);
      t.onerror = () => reject(t.error);
    });
  }

  function describe(url, fields) {
    const path = new URL(url).pathname;
    const get = (k) => (fields.find(([n]) => n === k) || [])[1] || "";
    const first = (get("title") || get("body") || get("text") || get("name")).split("\n")[0].slice(0, 60);
    if (path === "/notas") return "Nueva nota: " + first;
    if (/^\/notas\/\d+\/hecho$/.test(path)) return "Marcar pendiente hecho / no hecho";
    if (/^\/notas\/\d+\/fijar$/.test(path)) return "Fijar / desfijar nota";
    if (/^\/notas\/\d+\/borrar$/.test(path)) return "Borrar nota";
    if (/^\/notas\/\d+$/.test(path)) return "Editar nota: " + first;
    if (path === "/proyectos") return "Nuevo proyecto: " + first;
    if (path === "/planes") return "Nuevo plan: " + first;
    if (/^\/planes\/\d+\/pasos$/.test(path)) return "Nuevo paso: " + first;
    if (/^\/pasos\//.test(path)) return "Cambio en un paso del plan";
    return "Cambio en " + path;
  }

  scope.Outbox = {
    add: (url, fields) =>
      tx("readwrite", (s) => s.add({ url, fields, label: describe(url, fields), at: Date.now() })),
    all: () => tx("readonly", (s) => s.getAll()),
    remove: (id) => tx("readwrite", (s) => s.delete(id)),

    // Replays queued POSTs in order. Stops at the first network error (still offline)
    // or when the session expired (needs login). Returns {sent, left, needsLogin}.
    async flush() {
      const items = await this.all();
      let sent = 0;
      if (!items.length) return { sent, left: 0, needsLogin: false };
      let csrf;
      try {
        const r = await fetch("/api/csrf", { credentials: "same-origin", cache: "no-store" });
        if (r.status === 401) return { sent, left: items.length, needsLogin: true };
        csrf = (await r.json()).csrf;
      } catch (e) {
        return { sent, left: items.length, needsLogin: false };
      }
      for (const item of items) {
        const body = new URLSearchParams();
        for (const [k, v] of item.fields) body.append(k, k === "csrf" ? csrf : v);
        let res;
        try {
          res = await fetch(item.url, { method: "POST", body, credentials: "same-origin", redirect: "follow" });
        } catch (e) {
          break; // offline again
        }
        if (res.url.includes("/entrar")) return { sent, left: items.length - sent, needsLogin: true };
        // 2xx after redirect = applied; 4xx (e.g. note deleted meanwhile) can never succeed: drop it.
        await this.remove(item.id);
        sent += 1;
      }
      const left = (await this.all()).length;
      return { sent, left, needsLogin: false };
    },
  };
})(typeof self !== "undefined" ? self : window);
