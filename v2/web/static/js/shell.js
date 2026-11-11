// Shared page behaviour: sign-out, the language switch, toasts, menus, "/" for search, and saves whose
// answers were lost before a reload.
import { clearPending, newKey, save, send, settlePending } from "./api.js";

const toastEl = document.getElementById("toast");
const alertEl = document.getElementById("alert");
const ar = () => document.documentElement.lang === "ar";
let toastTimer;

// toast("Called in: Mona", {label: "Undo", onClick}) or {label, href}. Results go to a polite live region.
export function toast(text, action) {
  if (!toastEl) return;
  toastEl.querySelector("[data-text]").textContent = text;
  const button = toastEl.querySelector("[data-toast-action]");
  button.hidden = !action;
  button.onclick = null;
  if (action) {
    button.textContent = action.label;
    button.onclick = () => {
      toastEl.classList.remove("show");
      if (action.href) window.open(action.href, "_blank");
      if (action.onClick) action.onClick();
    };
  }
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), action ? 8000 : 4000);
}

// Errors stay until dismissed (design 7.6).
export function alertMessage(text) {
  if (!alertEl) return;
  alertEl.textContent = text;
  alertEl.classList.toggle("show", Boolean(text));
}
if (alertEl) alertEl.addEventListener("click", () => alertMessage(""));

document.addEventListener("click", async (event) => {
  // A menu closes when the person taps elsewhere.
  document.querySelectorAll("details.menu[open]").forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
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

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.querySelectorAll("details.menu[open]").forEach((menu) => { menu.open = false; });
  // "/" focuses search. Checked by key position: on the Arabic layout that key types ظ (design 7.6).
  if (event.code === "Slash" && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const target = event.target;
    if (target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    const search = document.querySelector("[data-quick-book] input, [data-search] input");
    if (search) { event.preventDefault(); search.focus(); }
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
  if (document.body.classList.contains("centered")) return;  // already on the sign-in page
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = "/login?next=" + next;
});

settlePending().then((results) => {
  if (!results.length) return;
  if (results.some((r) => r.expired)) {
    alertMessage(ar() ? "لم نتأكد من حفظ عملية سابقة. راجع السجل." : "We couldn't confirm an earlier save. Check the record.");
  } else if (results.some((r) => r.settled && r.status >= 200 && r.status < 300)) {
    toast(ar() ? "تم حفظ العمليات السابقة." : "The earlier saves went through.");
  }
  window.dispatchEvent(new CustomEvent("hamada:pending-settled"));
});
