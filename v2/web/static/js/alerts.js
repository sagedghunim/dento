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

// Next to an alert, its permission (Saged, 2026-11-25): green, the action waits for the owner's OK; red, it happens
// and the owner gets the alert. The whole list is saved with the one it replaces.
const gates = [...document.querySelectorAll("[data-gate]")];
if (gates.length) {
  const status = document.querySelector("[data-settings-status]");
  const listOf = () => gates.filter((g) => g.getAttribute("aria-pressed") === "true").map((g) => g.dataset.gate);
  gates.forEach((gate) => gate.addEventListener("click", async () => {
    if (gate.disabled) return;
    const before = listOf();
    const on = gate.getAttribute("aria-pressed") !== "true";
    gate.setAttribute("aria-pressed", on ? "true" : "false");
    const after = listOf();
    gate.disabled = true;
    status.textContent = t("saving");
    const result = await save("PUT", "/api/v1/settings/clinic", { changes: { approvals: after }, expected: { approvals: before } },
      { label: t("alert_settings") });
    gate.disabled = false;
    if (isOk(result)) { status.textContent = t("saved"); return; }
    gate.setAttribute("aria-pressed", on ? "false" : "true");
    status.textContent = errorMessage(result, t("failed"));
  }));
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
