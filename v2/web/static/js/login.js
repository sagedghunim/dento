import { errorMessage, send } from "./api.js";

const passwordForm = document.getElementById("password-form");
const codeForm = document.getElementById("code-form");

const NEXT = { home: "/", clinics: "/clinics", password: "/account/password", security: "/account/security", platform: "/platform" };

function go(next, form) {
  if (next === "code") {
    passwordForm.hidden = true;
    codeForm.hidden = false;
    codeForm.querySelector("input").focus();
    return;
  }
  const requested = form.dataset.next;
  window.location.href = next === "home" && requested && requested.startsWith("/") ? requested : (NEXT[next] || "/");
}

// Sign-in and the code take no request key: they are single-use by nature (design 5.0).
async function submit(form, url, body) {
  const error = form.querySelector("[data-error]");
  const button = form.querySelector('[type="submit"]');
  if (form.dataset.busy) return;
  form.dataset.busy = "1";
  button.disabled = true;
  error.textContent = "";
  const result = await send("POST", url, body);
  delete form.dataset.busy;
  button.disabled = false;
  if (result.status === 200) return go(result.data.next, form);
  if (result.status === 401 && url.endsWith("/code")) {
    codeForm.hidden = true;
    passwordForm.hidden = false;
  }
  error.textContent = errorMessage(result, document.documentElement.lang === "ar" ? "تعذر تسجيل الدخول." : "Couldn't sign in.");
}

passwordForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(passwordForm);
  submit(passwordForm, "/api/v1/auth/login", { username: data.get("username"), password: data.get("password") });
});
// Back from the code: the half sign-in ends, and the account and password come back (Saged, 2026-11-19).
codeForm.querySelector("[data-back-to-password]").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  await send("POST", "/api/v1/auth/logout", {});
  event.target.disabled = false;
  codeForm.reset();
  codeForm.querySelector("[data-error]").textContent = "";
  codeForm.hidden = true;
  passwordForm.hidden = false;
  passwordForm.querySelector("input").focus();
});

codeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submit(codeForm, "/api/v1/auth/code", { code: new FormData(codeForm).get("code") });
});
