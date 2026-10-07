// Copy buttons and confirmations (progressive enhancement: forms work without JS).
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  if (!source || !navigator.clipboard) return;
  navigator.clipboard.writeText(source.textContent.trim()).then(() => {
    const label = button.textContent;
    button.textContent = "¡Copiado!";
    setTimeout(() => { button.textContent = label; }, 1500);
  });
});

document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

document.addEventListener("submit", (event) => {
  const button = event.target.querySelector("button[type=submit]");
  if (button && !event.defaultPrevented) {
    setTimeout(() => { button.disabled = true; }, 0);
  }
});
