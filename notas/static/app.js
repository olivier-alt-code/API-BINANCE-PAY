// Small enhancements; every form also works without JavaScript.
(() => {
  const autogrow = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; };
  document.querySelectorAll("[data-autogrow]").forEach((el) => {
    el.addEventListener("input", () => autogrow(el));
  });

  document.addEventListener("keydown", (e) => {
    const el = e.target;
    if (el.matches && el.matches("[data-submit-ctrl-enter]") && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      el.form.requestSubmit();
    }
    // "/" focuses the global search (when not typing somewhere).
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      const search = document.querySelector("[data-hotkey='/']");
      if (search) { e.preventDefault(); search.focus(); }
    }
  });

  const flashButton = (btn, label) => {
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    const old = btn.innerHTML;
    btn.classList.add("copied");
    btn.textContent = label;
    setTimeout(() => { btn.innerHTML = old; btn.classList.remove("copied"); delete btn.dataset.busy; }, 1200);
  };

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.copyValue !== undefined) {
      navigator.clipboard.writeText(btn.dataset.copyValue).then(() => flashButton(btn, "¡Copiado!"));
      // Clear the clipboard after 30 s if it still holds the secret (best effort).
      const value = btn.dataset.copyValue;
      setTimeout(() => {
        navigator.clipboard.readText?.().then((t) => { if (t === value) navigator.clipboard.writeText(""); }).catch(() => {});
      }, 30000);
    }
    if (btn.hasAttribute("data-reveal")) {
      const secret = btn.parentElement.querySelector("[data-secret]");
      const shown = secret.textContent === secret.dataset.secret;
      secret.textContent = shown ? "••••••••" : secret.dataset.secret;
      secret.classList.toggle("shown", !shown);
      btn.setAttribute("aria-pressed", String(!shown));
    }
    if (btn.dataset.togglePassword) {
      const input = document.getElementById(btn.dataset.togglePassword);
      input.type = input.type === "password" ? "text" : "password";
      btn.setAttribute("aria-pressed", String(input.type === "text"));
    }
    if (btn.dataset.generate) {
      const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%&*-_=+?";
      const bytes = crypto.getRandomValues(new Uint32Array(20));
      const input = document.getElementById(btn.dataset.generate);
      input.value = Array.from(bytes, (b) => chars[b % chars.length]).join("");
      input.type = "text";
      input.classList.add("flash-in");
      setTimeout(() => input.classList.remove("flash-in"), 600);
    }
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();

// ---------------------------------------------------------------------------------------
// Offline support: service worker, outbox panel, drafts, pre-download of pages.
// ---------------------------------------------------------------------------------------
(() => {
  const logged = document.body.dataset.logged === "1";
  const statusBar = document.getElementById("net-status");
  const panel = document.getElementById("outbox");
  const list = document.getElementById("outbox-list");

  function setStatus(text, kind) {
    if (!statusBar) return;
    statusBar.textContent = text || "";
    statusBar.className = "netbar " + (kind || "");
    statusBar.hidden = !text;
  }

  async function renderOutbox() {
    if (!panel || !window.Outbox || !window.indexedDB) return 0;
    let items = [];
    try { items = await Outbox.all(); } catch (e) { return 0; }
    list.replaceChildren(...items.map((it) => {
      const li = document.createElement("li");
      li.textContent = it.label;
      const when = document.createElement("span");
      when.className = "muted small";
      when.textContent = " · " + new Date(it.at).toLocaleString();
      li.append(when);
      return li;
    }));
    panel.hidden = items.length === 0;
    return items.length;
  }

  function updateOnline() {
    if (!navigator.onLine) {
      setStatus("Sin conexión: ves la última copia guardada. Lo que guardes queda en cola.", "offline");
    } else {
      setStatus("");
    }
  }

  // Drafts: whatever you type in a text field survives reloads, crashes and offline.
  const draftKey = (el) => `draft:${location.pathname}:${el.form.getAttribute("action")}:${el.name}`;
  const draftFields = () => document.querySelectorAll("form textarea[name], form input[name=title]");
  function restoreDrafts() {
    draftFields().forEach((el) => {
      try {
        const saved = localStorage.getItem(draftKey(el));
        if (saved && !el.value) {
          el.value = saved;
          el.dispatchEvent(new Event("input"));
          el.classList.add("restored");
        }
      } catch (e) { /* storage disabled */ }
      el.addEventListener("input", () => {
        try {
          if (el.value) localStorage.setItem(draftKey(el), el.value);
          else localStorage.removeItem(draftKey(el));
        } catch (e) { /* quota */ }
      });
    });
  }
  document.addEventListener("submit", (e) => {
    if (e.defaultPrevented) return;
    e.target.querySelectorAll("textarea[name], input[name=title]").forEach((el) => {
      try { localStorage.removeItem(draftKey(el)); } catch (_) {}
    });
    if (e.target.hasAttribute("data-logout") && navigator.serviceWorker?.controller) {
      navigator.serviceWorker.controller.postMessage({ type: "clear-pages" });
    }
  });

  // Pre-download the pages linked from here (idle time, online, logged in).
  function warmCache(sw) {
    if (!logged || !navigator.onLine) return;
    const urls = new Set();
    document.querySelectorAll("a[href^='/']").forEach((a) => {
      const href = a.getAttribute("href");
      if (!/^\/(claves|entrar|salir|ajustes|configurar|static)/.test(href)) urls.add(href);
    });
    ["/", "/rutina", "/rutina?vista=todo", "/proyectos", "/planes", "/dinero"].forEach((u) => urls.add(u));
    try {
      const last = Number(localStorage.getItem("warmed-at") || 0);
      if (Date.now() - last < 5 * 60 * 1000) return; // at most every 5 min
      localStorage.setItem("warmed-at", String(Date.now()));
    } catch (e) { /* storage disabled: warm anyway */ }
    sw.postMessage({ type: "warm", urls: [...urls] });
  }

  async function flush() {
    const sw = navigator.serviceWorker?.controller;
    if (sw && navigator.onLine) sw.postMessage({ type: "flush" });
  }

  window.addEventListener("online", () => { updateOnline(); flush(); });
  window.addEventListener("offline", updateOnline);
  document.addEventListener("click", (e) => { if (e.target.closest("[data-flush]")) flush(); });

  updateOnline();
  restoreDrafts();
  renderOutbox().then((n) => {
    if (location.hash === "#en-cola") {
      setStatus(`Guardado sin conexión. ${n} cambio${n === 1 ? "" : "s"} esperando conexión para subirse.`, "queued");
      history.replaceState(null, "", location.pathname + location.search);
    }
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(async (reg) => {
      const ready = await navigator.serviceWorker.ready;
      if (ready.active) {
        if ("requestIdleCallback" in window) requestIdleCallback(() => warmCache(ready.active));
        else setTimeout(() => warmCache(ready.active), 1500);
        if ((await renderOutbox()) > 0) flush();
      }
      // Ask the browser to keep our storage (not evicted under pressure).
      navigator.storage?.persist?.();
      return reg;
    }).catch(() => {});

    navigator.serviceWorker.addEventListener("message", async (e) => {
      if (e.data?.type !== "synced") return;
      const left = await renderOutbox();
      if (e.data.needsLogin) setStatus("Hay cambios en cola: entra con tu contraseña para subirlos.", "queued");
      else if (e.data.sent) {
        setStatus(`✓ ${e.data.sent} cambio${e.data.sent === 1 ? "" : "s"} sincronizado${e.data.sent === 1 ? "" : "s"}.`, "ok");
        if (!left) setTimeout(() => location.reload(), 900);
      }
    });
  }
})();
