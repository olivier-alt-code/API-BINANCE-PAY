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
    const old = btn.textContent;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = old; }, 1200);
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
      btn.textContent = shown ? "Ver" : "Ocultar";
    }
    if (btn.dataset.togglePassword) {
      const input = document.getElementById(btn.dataset.togglePassword);
      input.type = input.type === "password" ? "text" : "password";
      btn.textContent = input.type === "password" ? "Ver" : "Ocultar";
    }
    if (btn.dataset.generate) {
      const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%&*-_=+?";
      const bytes = crypto.getRandomValues(new Uint32Array(20));
      const input = document.getElementById(btn.dataset.generate);
      input.value = Array.from(bytes, (b) => chars[b % chars.length]).join("");
      input.type = "text";
    }
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
