// The owner's alerts page: which kinds reach them (Saged, 2026-11-24). Each switch saves the whole list at once
// (PUT is absolute, so a resend changes nothing); a refused save puts the switch back.
import { errorMessage, isOk, save } from "./api.js";
import { toast } from "./shell.js";
import { t } from "./strings.js";

const box = document.querySelector("[data-alert-settings]");
if (box) {
  const status = box.querySelector("[data-settings-status]");
  box.addEventListener("change", async (event) => {
    const input = event.target.closest('[name="kind"]');
    if (!input) return;
    const off = [...box.querySelectorAll('[name="kind"]')].filter((c) => !c.checked).map((c) => c.value);
    status.textContent = t("saving");
    const result = await save("PUT", "/api/v1/alerts/settings", { off }, { label: t("alert_settings"), auto: true });
    if (isOk(result)) { status.textContent = t("saved"); return; }
    input.checked = !input.checked;
    status.textContent = errorMessage(result, t("failed"));
  });
}

// The desk's money out of the drawer: at once with an alert, or waiting for the owner's OK (Saged, 2026-11-25).
const approval = document.querySelector("[data-drawer-approval]");
if (approval) {
  const status = document.querySelector("[data-settings-status]");
  approval.addEventListener("change", async (event) => {
    const input = event.target.closest('[name="drawer_approval"]');
    if (!input) return;
    const on = input.value === "1";
    status.textContent = t("saving");
    const result = await save("PUT", "/api/v1/settings/clinic",
      { changes: { drawer_approval: on }, expected: { drawer_approval: !on } }, { label: t("alert_settings") });
    if (isOk(result)) { status.textContent = t("saved"); approval.dataset.on = on ? "1" : "0"; return; }
    approval.querySelector(`[value="${on ? "0" : "1"}"]`).checked = true;
    status.textContent = errorMessage(result, t("failed"));
  });
}

// Approve or refuse a request; the row goes once it is decided.
const waiting = document.querySelector("[data-cash-requests]");
if (waiting) {
  waiting.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-decide]");
    if (!button) return;
    const row = button.closest("[data-request]");
    const error = row.querySelector("[data-request-error]");
    row.querySelectorAll("[data-decide]").forEach((b) => { b.disabled = true; });
    error.textContent = "";
    const approve = button.dataset.decide === "approve";
    const result = await save("POST", `/api/v1/cash-requests/${row.dataset.request}/decide`, { approve },
      { label: t(approve ? "request_approved" : "request_refused") });
    const code = result.data && result.data.error && result.data.error.code;
    if (isOk(result) || code === "decided") {
      toast(t(approve ? "request_approved" : "request_refused"));
      row.remove();
      if (!waiting.querySelector("[data-request]")) waiting.remove();
      return;
    }
    row.querySelectorAll("[data-decide]").forEach((b) => { b.disabled = false; });
    error.textContent = errorMessage(result, t("failed"));
  });
}
