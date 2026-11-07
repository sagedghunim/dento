import { errorMessage, newKey, save } from "./api.js";

const error = document.querySelector("[data-error]");
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-pick]");
  if (!button || button.disabled) return;
  button.disabled = true;
  const result = await save("POST", "/api/v1/me/clinic", { membership_id: Number(button.dataset.pick) }, { key: newKey() });
  button.disabled = false;
  if (result.status === 200) { window.location.href = "/"; return; }
  if (result.status === 403 && result.data.error && result.data.error.code === "mfa_setup_required") {
    window.location.href = "/account/security";
    return;
  }
  error.textContent = errorMessage(result, document.documentElement.lang === "ar" ? "تعذر فتح العيادة." : "Couldn't open the clinic.");
});
