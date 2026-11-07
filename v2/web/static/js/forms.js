// A form that saves through api.save(): fields locked while the outcome is unknown, one key per form.
import { errorMessage, newKey, save } from "./api.js";

const lang = () => document.documentElement.lang;
const T = {
  saving: { ar: "جارٍ الحفظ…", en: "Saving…" },
  checking: { ar: "لم يتأكد الحفظ بعد، جارٍ التحقق…", en: "Not confirmed yet, checking…" },
  unknown: { ar: "لم نتأكد من الحفظ. حاول مرة أخرى، ولن يتكرر.", en: "Not confirmed. Try again: it won't be done twice." },
  failed: { ar: "حدث خطأ. حاول مرة أخرى.", en: "Something went wrong. Try again." },
};
const t = (k) => T[k][lang()] || T[k].en;

export function bindForm(form, { url, method = "POST", body, onSaved }) {
  let key = newKey();
  const errorEl = form.querySelector("[data-error]");
  const submit = form.querySelector('[type="submit"]');
  const label = submit ? submit.textContent : "";
  form.addEventListener("input", () => { if (errorEl) errorEl.textContent = ""; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.busy) return;            // a double tap does nothing
    form.dataset.busy = "1";
    const fields = Array.from(form.elements);
    fields.forEach((el) => { el.readOnly = true; });
    if (submit) { submit.disabled = true; submit.textContent = t("saving"); }
    if (errorEl) errorEl.textContent = "";
    const result = await save(method, url, body(new FormData(form)), {
      key, onWaiting: () => { if (submit) submit.textContent = t("checking"); },
    });
    delete form.dataset.busy;
    fields.forEach((el) => { el.readOnly = false; });
    if (submit) { submit.disabled = false; submit.textContent = label; }
    if (!result.settled) {                     // outcome unknown: keep the key, so a retry can't act twice
      if (errorEl) errorEl.textContent = t("unknown");
      return;
    }
    if (result.status >= 200 && result.status < 300) {
      key = newKey();
      onSaved(result.data);
      return;
    }
    key = newKey();                            // a 4xx wrote nothing: the next try is a new request
    if (errorEl) errorEl.textContent = errorMessage(result, t("failed"));
  });
}
