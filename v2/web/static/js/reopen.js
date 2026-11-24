// "Back to the visit" (Saged, 2026-11-24): for five minutes after the visit screen's finish, Today shows the visit
// just finished with its timer still running, and one tap puts it back in the room (POST appointments/{id}/reopen),
// in case the finish was tapped by mistake. The visit screen leaves {id, name, called_at, finished_at, skew} in
// sessionStorage ("hamada.finished") as it leaves; the server checks the five minutes again.
import { errorMessage, isOk, save } from "./api.js";
import { el } from "./dialogs.js";
import { localDigits } from "./numbers.js";
import { alertMessage } from "./shell.js";
import { t } from "./strings.js";

const KEY = "hamada.finished";
const WINDOW_MS = 5 * 60 * 1000;

function clock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return localDigits(h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`);
}

export function showReopen() {
  let visit = null;
  try { visit = JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { visit = null; }
  if (!visit || !visit.id) return;
  const skew = visit.skew || 0;
  const calledAt = Date.parse(visit.called_at);
  const forget = () => { try { sessionStorage.removeItem(KEY); } catch (e) { /* none */ } };
  if (Date.now() + skew - visit.finished_at > WINDOW_MS) { forget(); return; }

  const bar = el("div", "notice reopen-bar");
  bar.setAttribute("role", "status");
  const text = el("span", null, t("reopen_text", { name: visit.name || "" }));
  const timer = el("strong", "ltr num reopen-timer");
  const button = el("button", "btn", t("reopen_back"));
  button.type = "button";
  const dismiss = el("button", "icon-btn", "×");
  dismiss.type = "button";
  dismiss.setAttribute("aria-label", t("reopen_close"));
  bar.append(text, " ", timer, " ", button, dismiss);
  const main = document.getElementById("main");
  if (!main) return;
  main.prepend(bar);

  let ticking = null;
  function tick() {
    const now = Date.now() + skew;
    if (now - visit.finished_at > WINDOW_MS) { stop(); return; }
    timer.textContent = Number.isNaN(calledAt) ? "" : clock(now - calledAt);
  }
  function stop() { clearInterval(ticking); forget(); bar.remove(); }
  tick();
  ticking = setInterval(tick, 1000);
  dismiss.addEventListener("click", stop);
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    const result = await save("POST", `/api/v1/appointments/${visit.id}/reopen`, { status: "done" },
      { label: t("reopen_back") });
    if (result.settled && isOk(result)) {
      forget();
      window.location.assign(`/visits/${visit.id}`);
      return;
    }
    button.disabled = false;
    if (result.settled) {
      alertMessage(errorMessage(result, t("failed")));
      const code = result.data && result.data.error && result.data.error.code;
      if (code === "too_late" || code === "status_changed") stop();
    }
  });
}
