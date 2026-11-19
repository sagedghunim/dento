// Shared page behaviour: sign-out, the language switch, toasts, menus, "/" for search, and saves whose
// answers were lost before a reload.
import { clearPending, discardPending, errorMessage, isOk, newKey, pendingList, retryPending, save, send, settlePending } from "./api.js";
import { t } from "./strings.js";

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
  if (button.dataset.action === "back") { window.history.back(); return; }
  if (button.dataset.action === "logout") {
    button.disabled = true;
    await send("POST", "/api/v1/auth/logout", {});
    clearPending();
    try { new BroadcastChannel("hamada").postMessage({ type: "signed-out" }); } catch (e) { /* old browser */ }
    window.location.href = "/login";
  }
  if (button.dataset.action === "language") {
    button.disabled = true;
    // The cookie keeps the choice on the signed-out pages too (core/middleware.py reads it before sign-in).
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `hamada_language=${button.dataset.language}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
    if (!("signedOut" in button.dataset)) {
      await save("POST", "/api/v1/me/language", { language: button.dataset.language }, { key: newKey() });
    }
    window.location.reload();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.querySelectorAll("details.menu[open]").forEach((menu) => { menu.open = false; });
  // "/" focuses search. Checked by key position: on the Arabic layout that key types ظ (design 7.6).
  if (event.code === "Slash" && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const target = event.target;
    if (target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    const search = document.querySelector("[data-search] input");
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

// --- saves whose outcome is unknown: listed on every clinic page with "Try again" and "Discard" only ---
// (design 7.4, M1 decision R7). Nothing here is resent without the person asking.

function pendingBar() {
  let bar = document.querySelector("[data-pending-bar]");
  if (!bar) {
    const topbar = document.querySelector(".wrap > .topbar");
    if (!topbar) return null;
    bar = document.createElement("div");
    bar.className = "pending-bar";
    bar.dataset.pendingBar = "";
    topbar.after(bar);
  }
  return bar;
}

function button(text, className, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}

export function renderPending() {
  const items = pendingList();
  const bar = items.length ? pendingBar() : document.querySelector("[data-pending-bar]");
  if (!bar) return;
  bar.hidden = !items.length;
  bar.replaceChildren(...items.map((p) => {
    const box = document.createElement("div");
    box.className = "notice pending-item";
    box.setAttribute("role", "alert");
    const text = document.createElement("span");
    text.textContent = t("not_confirmed", { what: p.label || "" });
    const actions = document.createElement("span");
    actions.className = "appt-actions";
    const again = button(t("try_again"), "btn", async () => {
      again.disabled = true;
      const result = await retryPending(p.key);  // the same key and body: never done twice
      if (result && result.expired) alertMessage(t("check_record"));
      else if (result && result.settled && !isOk(result)) alertMessage(errorMessage(result, t("failed")));
      else if (result && result.settled) toast(t("saved"));
      renderPending();
      window.dispatchEvent(new CustomEvent("hamada:pending-settled"));
    });
    const drop = button(t("discard"), "btn ghost", () => { discardPending(p.key); renderPending(); alertMessage(t("check_record")); });
    actions.append(again, drop);
    box.append(text, actions);
    return box;
  }));
}

window.addEventListener("hamada:pending-changed", () => renderPending());

settlePending().then((results) => {
  renderPending();
  if (!results.length) return;
  if (results.some((r) => r.expired)) {
    alertMessage(ar() ? "لم نتأكد من حفظ عملية سابقة. راجع السجل." : "We couldn't confirm an earlier save. Check the record.");
  } else if (results.some((r) => r.settled && r.status >= 200 && r.status < 300)) {
    toast(ar() ? "تم حفظ العمليات السابقة." : "The earlier saves went through.");
  }
  window.dispatchEvent(new CustomEvent("hamada:pending-settled"));
});

// The back arrow shows when the page before is one of ours (not the sign-in page, not another site).
const backButton = document.querySelector('[data-action="back"]');
if (backButton) {
  let from = null;
  try { from = document.referrer ? new URL(document.referrer) : null; } catch (e) { from = null; }
  backButton.hidden = !(from && from.origin === window.location.origin && from.pathname !== "/login" && window.history.length > 1);
}

// --- parts of a page from the side bar (Saged, 2026-11-19): ?part= shows that part alone; the menu marks it ---
{
  const here = new URLSearchParams(window.location.search);
  const part = here.get("part");
  const parts = [...document.querySelectorAll("main [data-part]")];
  if (part && parts.some((p) => p.dataset.part === part)) {
    parts.forEach((p) => { if (p.dataset.part !== part) p.hidden = true; });
    document.body.classList.add("one-part");
  }
  const path = window.location.pathname;
  const key = part || here.get("tab");
  document.querySelectorAll("[data-nav-part]").forEach((link) => {
    const url = new URL(link.href, window.location.href);
    const linkKey = url.searchParams.get("part") || url.searchParams.get("tab");
    if (url.pathname === path && linkKey === key) link.setAttribute("aria-current", "page");
  });
}
