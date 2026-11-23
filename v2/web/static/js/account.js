import { errorMessage, get, isOk, send } from "./api.js";
import { bindForm } from "./forms.js";

const NEXT = { home: "/", clinics: "/clinics", password: "/account/password", security: "/account/security", platform: "/platform" };

const passwordForm = document.getElementById("password-change");
if (passwordForm) {
  bindForm(passwordForm, {
    url: "/api/v1/me/password",
    body: (data) => ({ current: data.get("current"), new: data.get("new") }),
    onSaved: (data) => { window.location.href = NEXT[data.next] || "/"; },
  });
}

function showCode(data) {
  // The SVG comes from our own server (segno), never from user input.
  const holder = document.getElementById("totp-qr");
  const svg = new DOMParser().parseFromString(data.qr_svg, "image/svg+xml").documentElement;
  holder.replaceChildren(document.importNode(svg, true));
  document.getElementById("totp-secret").textContent = data.secret.replace(/(.{4})/g, "$1 ").trim();
}

// Two-step sign-in already on: the same code again for another phone, after the password (Saged, 2026-11-22).
// No request key, like signing in: nothing is stored, so there is nothing to confirm or replay.
const showForm = document.getElementById("totp-show");
if (showForm) {
  const showError = showForm.querySelector("[data-error]");
  const showButton = showForm.querySelector('[type="submit"]');
  showForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (showButton.disabled) return;
    showButton.disabled = true;
    showError.textContent = "";
    const result = await send("POST", "/api/v1/me/totp/show", { password: new FormData(showForm).get("password") });
    showButton.disabled = false;
    if (isOk(result)) {
      showForm.hidden = true;
      showCode(result.data);
      document.getElementById("totp-again").hidden = false;
      return;
    }
    showError.textContent = errorMessage(result, document.documentElement.lang === "ar" ? "حدث خطأ. حاول مرة أخرى." : "Something went wrong. Try again.");
  });
}

const totpForm = document.getElementById("totp-confirm");
if (totpForm) {
  get("/api/v1/me/totp").then((result) => {
    if (result.status === 200) showCode(result.data);
  });
  bindForm(totpForm, {
    url: "/api/v1/me/totp/confirm",
    body: (data) => ({ code: data.get("code") }),
    onSaved: (data) => {
      totpForm.hidden = true;
      const list = document.getElementById("recovery-codes");
      list.replaceChildren(...data.recovery_codes.map((code) => {
        const li = document.createElement("li");
        li.textContent = code;
        return li;
      }));
      document.getElementById("recovery").hidden = false;
    },
  });
}
