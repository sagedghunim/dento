// Sign-up and "add a place" (design D2, D3): clinic or center, the specialties (a center's become
// sections with editable names), the place, and for a new owner the account. One request, one key
// until a definite answer. The password is never kept in sessionStorage, so this form uses send(),
// not save().
import { errorMessage, isOk, newKey, send } from "./api.js";
import { asciiDigits } from "./numbers.js";
import { t } from "./strings.js";

const form = document.getElementById("signup-form");
const mode = form.dataset.mode;
const field = (name) => form.elements.namedItem(name);
const names = form.querySelector("[data-section-names]");
const rowTemplate = document.getElementById("section-row");
const error = form.querySelector("[data-error]");
const submit = form.querySelector("[data-submit]");
const label = submit.textContent;
const order = [];            // specialties in the order they were picked
const typed = {};            // section names the person edited, by specialty
let key = newKey();
let unknown = null;          // the body sent when the outcome is unknown

const kind = () => form.querySelector('[name="kind"]:checked').value;
const picked = () => order.filter((code) => form.querySelector(`[name="specialty"][value="${code}"]`).checked);

function showKind() {
  const center = kind() === "center";
  form.querySelectorAll("[data-when-kind]").forEach((node) => { node.hidden = node.dataset.whenKind !== kind(); });
  if (!center) {
    const keep = picked()[0];
    form.querySelectorAll('[name="specialty"]').forEach((box) => { box.checked = box.value === keep; });
  }
  renderNames();
}

function renderNames() {
  const center = kind() === "center";
  const codes = picked();
  names.hidden = !center || !codes.length;
  names.replaceChildren(...(center ? codes : []).map((code) => {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const box = form.querySelector(`[name="specialty"][value="${code}"]`);
    row.querySelector("[data-label]").textContent = t("section_name", { name: box.dataset.name });
    const input = row.querySelector("input");
    input.value = typed[code] != null ? typed[code] : box.dataset.name;
    input.dataset.code = code;
    return row;
  }));
}

form.addEventListener("change", (event) => {
  const target = event.target;
  if (target.name === "kind") showKind();
  if (target.name === "specialty") {
    const at = order.indexOf(target.value);
    if (at >= 0) order.splice(at, 1);
    if (target.checked) order.push(target.value);
    if (target.checked && kind() === "clinic") {
      form.querySelectorAll('[name="specialty"]').forEach((box) => { if (box !== target) box.checked = false; });
    }
    renderNames();
  }
});
form.addEventListener("input", (event) => {
  if (event.target.name === "section_name") typed[event.target.dataset.code] = event.target.value;
  if (!unknown) { error.textContent = ""; clearFieldErrors(); }
});

function clearFieldErrors() {
  form.querySelectorAll("[data-field-error]").forEach((node) => { node.textContent = ""; });
  form.querySelectorAll("[aria-invalid]").forEach((node) => node.removeAttribute("aria-invalid"));
}

function body() {
  const codes = picked();
  const out = {
    kind: kind(),
    sections: codes.map((code) => {
      const input = names.querySelector(`input[data-code="${code}"]`);
      const name = input ? input.value.trim() : "";
      return name ? { specialty: code, name } : { specialty: code };
    }),
    name: field("name").value.trim(),
    city: field("city").value.trim(),
    phone: asciiDigits(field("phone").value).trim(),
  };
  if (mode === "signup") {
    out.owner = {
      full_name: field("owner_full_name").value.trim(),
      mobile: asciiDigits(field("owner_mobile").value).trim(),
      username: field("owner_username").value.trim().toLowerCase(),
      password: field("owner_password").value,
    };
    out.website = field("website").value;  // the honeypot: only a bot fills it
  }
  return out;
}

function showFieldErrors(fields, message) {
  let first = null;
  for (const name of Object.keys(fields || {})) {
    let holder = form.querySelector(`[data-field-error="${name}"]`);
    let input = null;
    const section = /^sections\.(\d+)\./.exec(name);
    if (section) {
      input = names.querySelectorAll("input")[Number(section[1])] || null;
      holder = form.querySelector('[data-field-error="sections"]');
    } else if (holder) {
      input = holder.closest(".field") && holder.closest(".field").querySelector("input");
    } else if (name.startsWith("sections")) {
      holder = form.querySelector('[data-field-error="sections"]');
    }
    if (holder) holder.textContent = fields[name] === "taken" ? t("taken") : message;
    if (input) { input.setAttribute("aria-invalid", "true"); first = first || input; }
    else if (holder) first = first || holder;
  }
  if (first && first.focus) first.focus();
  return Boolean(first);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (form.dataset.busy) return;  // a double tap does nothing
  error.textContent = "";
  const sending = unknown || body();
  if (!unknown) {
    clearFieldErrors();
    const count = sending.sections.length;
    if (sending.kind === "clinic" && count !== 1) { error.textContent = t("pick_one"); return; }
    if (sending.kind === "center" && count < 2) { error.textContent = t("pick_two"); return; }
  }
  form.dataset.busy = "1";
  submit.disabled = true;
  submit.textContent = t("saving");
  const url = mode === "signup" ? "/api/v1/signup" : "/api/v1/me/places";
  const result = await send("POST", url, sending, { key, onWaiting: () => { submit.textContent = t("checking"); } });
  delete form.dataset.busy;
  submit.disabled = false;
  submit.textContent = label;
  if (!result.settled) {  // unknown: the same key and body again, never a second place
    unknown = sending;
    error.textContent = t("unknown");
    return;
  }
  unknown = null;
  key = newKey();
  if (isOk(result)) {
    const next = result.data.next;
    window.location.href = next === "home" ? "/" : next === "clinics" ? "/clinics" : "/account/security";
    return;
  }
  const fields = result.data.error && result.data.error.fields;
  const message = errorMessage(result, t("failed"));
  error.textContent = showFieldErrors(fields, message) ? t("fix_fields") + " " + message : message;
});

showKind();
