// The owner's alerts page: which kinds reach them (Saged, 2026-11-24). Each switch saves the whole list at once
// (PUT is absolute, so a resend changes nothing); a refused save puts the switch back.
import { errorMessage, isOk, save } from "./api.js";
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
