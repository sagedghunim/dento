// Shared page behaviour: sign-out, the language switch, toasts, and a lost save from before a reload.
import { clearPending, newKey, save, send, settlePending } from "./api.js";

const toastEl = document.getElementById("toast");
const alertEl = document.getElementById("alert");
let toastTimer;

export function toast(text) {
  if (!toastEl) return;
  toastEl.textContent = text;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 4000);
}

// Errors stay until dismissed (design 7.6).
export function alertMessage(text) {
  if (!alertEl) return;
  alertEl.textContent = text;
  alertEl.classList.toggle("show", Boolean(text));
}
if (alertEl) alertEl.addEventListener("click", () => alertMessage(""));

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  if (button.dataset.action === "logout") {
    button.disabled = true;
    await send("POST", "/api/v1/auth/logout", {});
    clearPending();
    try { new BroadcastChannel("hamada").postMessage({ type: "signed-out" }); } catch (e) { /* old browser */ }
    window.location.href = "/login";
  }
  if (button.dataset.action === "language") {
    button.disabled = true;
    await save("POST", "/api/v1/me/language", { language: button.dataset.language }, { key: newKey() });
    window.location.reload();
  }
});

// Sign-in and sign-out in another tab (design 7.4).
try {
  new BroadcastChannel("hamada").addEventListener("message", (event) => {
    if (event.data && event.data.type === "signed-out" && !document.body.classList.contains("centered")) {
      window.location.href = "/login";
    }
  });
} catch (e) { /* BroadcastChannel missing */ }

window.addEventListener("hamada:signed-out", () => {
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = "/login?next=" + next;
});

settlePending().then((result) => {
  if (!result) return;
  if (result.expired) alertMessage(document.documentElement.lang === "ar"
    ? "لم نتأكد من حفظ آخر عملية. راجع السجل."
    : "We couldn't confirm the last save. Check the record.");
  else if (result.settled && result.status >= 200 && result.status < 300) toast(document.documentElement.lang === "ar" ? "تم حفظ آخر عملية." : "The last save went through.");
});
