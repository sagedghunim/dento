import { get } from "./api.js";
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
const showForm = document.getElementById("totp-show");
if (showForm) {
  bindForm(showForm, {
    url: "/api/v1/me/totp/show",
    body: (data) => ({ password: data.get("password") }),
    onSaved: (data) => {
      showForm.hidden = true;
      showCode(data);
      document.getElementById("totp-again").hidden = false;
    },
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
