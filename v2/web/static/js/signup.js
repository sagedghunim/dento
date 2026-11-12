// Sign-up and "add a place" (design D2, E2): clinic or center, then the specialty cards. A clinic picks
// one card; a center gives each card a count with − and + (0 to 20, at least 2 in all), and each count
// becomes that many sections with generated, editable names. A center also says whether its sections are
// one place (`one_place`) or separate branches (`branches`). Then the place and, for a new owner, the account.
// One request, one key until a definite answer. The password is never kept in sessionStorage, so this form
// uses send(), not save().
import { errorMessage, isOk, newKey, send } from "./api.js";
import { asciiDigits, localDigits } from "./numbers.js";
import { t } from "./strings.js";

const MAX_SECTIONS = 20;
const form = document.getElementById("signup-form");
const mode = form.dataset.mode;
const field = (name) => form.elements.namedItem(name);
const names = form.querySelector("[data-section-names]");
const rowTemplate = document.getElementById("section-row");
const error = form.querySelector("[data-error]");
const submit = form.querySelector("[data-submit]");
const label = submit.textContent;
const cards = [...form.querySelectorAll(".spec-card")];  // in the order design E2 gives
const counts = Object.fromEntries(cards.map((card) => [card.dataset.code, 0]));
const typed = {};            // section names the person edited, by "code#n"
let key = newKey();
let unknown = null;          // the body sent when the outcome is unknown

const kind = () => form.querySelector('[name="kind"]:checked').value;
const layout = () => (form.querySelector('[name="layout"]:checked') || {}).value || "one_place";
const total = () => Object.values(counts).reduce((a, b) => a + b, 0);
const cardOf = (code) => cards.find((card) => card.dataset.code === code);

// A count of 1 gives the specialty's name; N gives "Name 1" … "Name N" (design E2).
function sectionsWanted() {
  if (kind() === "clinic") {
    const pick = form.querySelector('[name="specialty"]:checked');
    return pick ? [{ code: pick.value, n: 1, of: 1 }] : [];
  }
  return cards.flatMap((card) => Array.from({ length: counts[card.dataset.code] }, (_, i) => ({ code: card.dataset.code, n: i + 1, of: counts[card.dataset.code] })));
}

const generated = (s) => (s.of === 1 ? cardOf(s.code).dataset.name : `${cardOf(s.code).dataset.name} ${localDigits(s.n)}`);

function showKind() {
  const center = kind() === "center";
  form.querySelectorAll("[data-when-kind]").forEach((node) => { node.hidden = node.dataset.whenKind !== kind(); });
  if (center) form.querySelectorAll('[name="specialty"]').forEach((radio) => { radio.checked = false; });
  render();
}

function render() {
  const center = kind() === "center";
  cards.forEach((card) => {
    const n = counts[card.dataset.code];
    card.querySelector("[data-count]").textContent = localDigits(n);
    card.classList.toggle("on", center && n > 0);
    card.querySelector('[data-step="-1"]').disabled = n === 0;
    card.querySelector('[data-step="1"]').disabled = n >= MAX_SECTIONS || total() >= MAX_SECTIONS;
  });
  const wanted = center ? sectionsWanted() : [];
  names.hidden = !wanted.length;
  names.replaceChildren(...wanted.map((s) => {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const id = `${s.code}#${s.n}`;
    row.querySelector("[data-label]").textContent = t("section_name", { name: generated(s) });
    const input = row.querySelector("input");
    input.value = typed[id] != null ? typed[id] : generated(s);
    input.dataset.id = id;
    return row;
  }));
}

form.addEventListener("click", (event) => {
  const step = event.target.closest("[data-step]");
  if (!step || step.disabled || unknown) return;
  const code = step.closest(".spec-card").dataset.code;
  counts[code] = Math.max(0, Math.min(MAX_SECTIONS, counts[code] + Number(step.dataset.step)));
  render();
});
form.addEventListener("change", (event) => {
  if (event.target.name === "kind") showKind();
});
form.addEventListener("input", (event) => {
  if (event.target.name === "section_name") typed[event.target.dataset.id] = event.target.value;
  if (!unknown) { error.textContent = ""; clearFieldErrors(); }
});

function clearFieldErrors() {
  form.querySelectorAll("[data-field-error]").forEach((node) => { node.textContent = ""; });
  form.querySelectorAll("[aria-invalid]").forEach((node) => node.removeAttribute("aria-invalid"));
}

// One entry per section, each `count: 1` with its name: the names stay exactly as shown, and a server
// that predates `count` and `layout` still reads it as before (design D2).
function body() {
  const center = kind() === "center";
  const out = {
    kind: kind(),
    sections: sectionsWanted().map((s) => {
      if (!center) return { specialty: s.code, count: 1 };
      const input = names.querySelector(`input[data-id="${s.code}#${s.n}"]`);
      const name = (input ? input.value.trim() : "") || generated(s);
      return { specialty: s.code, count: 1, name };
    }),
    name: field("name").value.trim(),
    city: field("city").value.trim(),
    phone: asciiDigits(field("phone").value).trim(),
  };
  if (center) out.layout = layout();
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
    if (count > MAX_SECTIONS) { error.textContent = t("too_many"); return; }
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
