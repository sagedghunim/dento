import { errorMessage, newKey, save } from "./api.js";
import { bindForm } from "./forms.js";
import { alertMessage } from "./shell.js";

const form = document.getElementById("clinic-create");
bindForm(form, {
  url: "/api/v1/platform/clinics",
  body: (data) => Object.fromEntries(["name", "specialty", "city", "owner_username", "owner_name", "owner_phone"].map((k) => [k, data.get(k) || ""])),
  onSaved: (data) => {
    form.reset();
    document.getElementById("created-password").textContent = data.temporary_password || "";
    document.getElementById("created").hidden = !data.temporary_password;
  },
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-status]");
  if (!button || button.disabled) return;
  button.disabled = true;
  const result = await save("POST", `/api/v1/platform/clinics/${button.dataset.clinic}/${button.dataset.status}`, {}, { key: newKey() });
  if (result.status === 200) { window.location.reload(); return; }
  button.disabled = false;
  alertMessage(errorMessage(result, document.documentElement.lang === "ar" ? "تعذر التغيير." : "Couldn't change it."));
});
