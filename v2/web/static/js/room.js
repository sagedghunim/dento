// The doctor's visit screen (Saged, 2026-11-13): the timer, the notes saved as the doctor types, procedures,
// a prescription, the dental chart on demand, and "Finish the visit", the one way out.
// - Notes: PUT appointments/{id}/note with If-Match and a request key; the same text keeps its key until a
//   definite answer, so a resend after a lost answer never saves twice. The text waits in sessionStorage
//   (this tab only, cleared once saved) so a reload or a sign-in never loses it.
// - Leaving the page warns, except after the finish, a sign-out or a language switch.
import { discardPending, errorMessage, fetchText, get, isOk, membership, newKey, save, send } from "./api.js";
import { Submitter, el, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { asciiDigits, localDigits, moneyText } from "./numbers.js";
import { alertMessage, renderPending, toast } from "./shell.js";
import { soundsLike } from "./sounds.js";
import { t } from "./strings.js";

const data = JSON.parse(document.getElementById("visit-data").textContent);
const id = data.appointment_id;
const english = document.documentElement.lang === "en";
const nameOf = (p) => (english && p.name_en ? p.name_en : p.name_ar || p.name_en || p.name || "");
let leaving = false;

// --- the timer since call-in, by the server's clock ---

const skew = (Date.parse(data.server_time) || Date.now()) - Date.now();
const calledAt = Date.parse(data.called_at);
const timer = document.querySelector("[data-timer]");
function tick() {
  if (!timer || Number.isNaN(calledAt)) return;
  const total = Math.max(0, Math.floor((Date.now() + skew - calledAt) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  timer.textContent = localDigits(h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`);
}
tick();
setInterval(tick, 1000);

// The desk may finish this visit for the doctor (Saged, 2026-11-24): every 20 s the visit screen looks, and a visit
// no longer in the room goes to Today, where "Back to the visit" waits for five minutes.
setInterval(async () => {
  if (document.hidden || leaving) return;
  const result = await get(`/api/v1/appointments/${id}`);
  const status = isOk(result) && result.data.appointment ? result.data.appointment.status : null;
  if (!status || status === "in_room") return;
  if (box.value !== saved) await flush();
  leaving = true;
  window.location.assign("/");
}, 20000);

// --- the notes ---

const box = document.querySelector("[data-notes]");
const state = document.querySelector("[data-save-state]");
const conflict = document.querySelector("[data-note-conflict]");
const draftKey = `hamada.note.${membership()}.${id}`;
let version = data.note.version;
let saved = data.note.notes;            // the text the server holds
let others = { complaint: data.note.complaint, diagnosis: data.note.diagnosis };
let unknown = null;                     // {key, body, version} whose answer was lost
const sent = new Set();                 // texts this screen sent: a 409 showing one of them is our own save
let saving = null;
let timerId = null;
let retryId = null;

function say(text, bad) { state.textContent = text; state.classList.toggle("bad", Boolean(bad)); }
function keepDraft() {
  try {
    if (box.value === saved) sessionStorage.removeItem(draftKey);
    else sessionStorage.setItem(draftKey, JSON.stringify({ text: box.value, base: version }));
  } catch (e) { /* private mode */ }
}
try {
  const draft = JSON.parse(sessionStorage.getItem(draftKey) || "null");
  if (draft && typeof draft.text === "string" && draft.text !== saved) {
    if (draft.base !== version && saved) showTheirs(saved);  // both changed: theirs stays readable
    box.value = draft.text;
  }
} catch (e) { /* none */ }

function showTheirs(text) {
  conflict.hidden = false;
  conflict.querySelector("[data-their-text]").textContent = text;
}

async function saveOnce() {
  const text = box.value;
  if (text === saved) { say(t("note_saved")); keepDraft(); return true; }
  const body = { ...others, notes: text };
  const request = unknown && unknown.body.notes === text && unknown.version === version
    ? unknown : { key: newKey(), body, version };
  sent.add(text);
  say(t("note_saving"));
  const result = await send("PUT", `/api/v1/appointments/${id}/note`, request.body,
    { key: request.key, headers: { "If-Match": String(request.version) } });
  if (!result.settled) {
    unknown = request;
    if (!result.signedOut) say(t("note_offline"), true);
    return false;
  }
  unknown = null;
  if (isOk(result)) {
    const note = result.data.note;
    version = note.version;
    saved = note.notes;
    others = { complaint: note.complaint, diagnosis: note.diagnosis };
    keepDraft();
    if (box.value === saved) { say(t("note_saved")); return true; }
    return saveOnce();  // typed meanwhile
  }
  const error = result.data && result.data.error;
  if (result.status === 409 && error && error.code === "stale") {
    const theirs = error.note;
    version = theirs ? theirs.version : 0;
    if (theirs) {
      others = { complaint: theirs.complaint, diagnosis: theirs.diagnosis };
      saved = theirs.notes;
      if (!sent.has(theirs.notes)) showTheirs(theirs.notes);
    }
    return saveOnce();
  }
  say(errorMessage(result, t("failed")), true);
  return false;
}

// One save at a time; a change during a save is saved right after it.
function flush() {
  clearTimeout(timerId);
  if (!saving) saving = saveOnce().finally(() => { saving = null; });
  return saving.then((ok) => {
    if (!ok && unknown) { clearTimeout(retryId); retryId = setTimeout(flush, 5000); }  // only a lost answer is resent
    return ok && box.value === saved;
  });
}

box.addEventListener("input", () => {
  keepDraft();
  if (!state.classList.contains("bad")) say("");
  clearTimeout(timerId);
  timerId = setTimeout(flush, 1200);
});
box.addEventListener("blur", () => { if (box.value !== saved) flush(); });
// The chart's findings, written into the notes and saved (Saged, 2026-11-24): the doctor needn't type them.
let charted = Boolean(data.charted);
window.addEventListener("hamada:teeth-noted", (event) => {
  const lines = event.detail.lines || [];
  if (!lines.length) return;
  charted = true;
  const text = box.value.replace(/\s+$/, "");
  box.value = (text ? `${text}\n` : "") + lines.join("\n");
  keepDraft();
  flush();
});
window.addEventListener("online", () => { if (box.value !== saved) flush(); });
if (box.value !== saved) flush(); else if (saved) say(t("note_saved"));

// --- the patient's medical file: chronic conditions, allergies, notes (Saged, 2026-11-23) ---
// Saved to the file as the doctor types, one field at a time, with the value it replaces (a change made meanwhile on
// another screen is refused, not overwritten). A lost answer resends the same request under the same key.

const medical = document.querySelector("[data-medical]");
let flushMedical = async () => true;     // the finish saves what was typed first
if (medical) {
  const medState = medical.querySelector("[data-medical-state]");
  const medSaved = { ...data.medical };
  const medLost = {};                    // field -> {key, body} whose answer was lost
  const medTimers = {};
  let medBusy = Promise.resolve();
  const sayMed = (text, bad) => { medState.textContent = text; medState.classList.toggle("bad", Boolean(bad)); };

  async function saveField(name) {
    const area = medical.querySelector(`[name="${name}"]`);
    const value = area.value.trim();
    if (value === medSaved[name]) return true;
    const lost = medLost[name];
    const request = lost && lost.body.changes[name] === value
      ? lost : { key: newKey(), body: { changes: { [name]: value }, expected: { [name]: medSaved[name] } } };
    sayMed(t("medical_saving"));
    const result = await send("PATCH", `/api/v1/patients/${data.patient_id}`, request.body, { key: request.key });
    if (!result.settled) {
      medLost[name] = request;
      if (!result.signedOut) sayMed(t("medical_offline"), true);
      clearTimeout(medTimers[name]);
      medTimers[name] = setTimeout(() => queueField(name), 5000);
      return false;
    }
    delete medLost[name];
    if (!isOk(result)) { sayMed(errorMessage(result, t("failed")), true); return false; }
    medSaved[name] = value;
    if (name !== "notes") area.closest(".field").classList.toggle(name === "allergies" ? "flag-allergy" : "flag-chronic", Boolean(value));
    if (area.value.trim() !== value) return saveField(name);  // typed meanwhile (already inside the queue)
    sayMed(t("medical_saved"));
    return true;
  }
  // One save at a time across the three fields.
  function queueField(name) {
    clearTimeout(medTimers[name]);
    medBusy = medBusy.then(() => saveField(name));
    return medBusy;
  }
  medical.addEventListener("input", (event) => {
    const name = event.target.name;
    if (!(name in medSaved)) return;
    if (!medState.classList.contains("bad")) sayMed("");
    clearTimeout(medTimers[name]);
    medTimers[name] = setTimeout(() => queueField(name), 1200);
  });
  medical.addEventListener("focusout", (event) => {
    const name = event.target.name;
    if (name in medSaved && event.target.value.trim() !== medSaved[name]) queueField(name);
  });
  window.addEventListener("online", () => { for (const name of Object.keys(medLost)) queueField(name); });
  flushMedical = async () => {
    for (const name of Object.keys(medSaved)) {
      if (medical.querySelector(`[name="${name}"]`).value.trim() !== medSaved[name] && !(await queueField(name))) return false;
    }
    return true;
  };
}

// --- leaving warns: the doctor leaves by finishing the visit ---

window.addEventListener("beforeunload", (event) => {
  if (leaving) return;
  keepDraft();
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("hamada:signed-out", () => { leaving = true; }, { capture: true });
document.addEventListener("click", (event) => {
  if (event.target.closest('[data-action="logout"], [data-action="language"]')) leaving = true;
}, true);

// --- "Done today": the small bill. The six most used procedures are one tap; the rest are found by typing. ---
// The price is the clinic's list price: the screen never sends one, and the server refuses any other (price_locked).

const procList = document.querySelector("[data-procedures]");
const procTotal = document.querySelector("[data-procs-total]");
const offers = document.querySelector("[data-offers]");
const catalogue = new Map((data.procedures || []).map((p) => [p.id, p]));

// The search (Saged, 2026-11-17, 2026-11-23): from one letter, Arabic or English, either name or the section's name
// ("تج" finds the cosmetic ones). Spelling variants match (أ إ آ ا, ة ه, ى ي); a word that starts with what was typed
// comes first. Nothing typed is the whole list.
const plain = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
  .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");
const haystack = new Map([...catalogue.values()].map((p) => [p.id, plain(`${p.name} ${p.find || ""}`)]));
function rankText(text, q) {
  if (!text.includes(q)) return -1;
  return text.startsWith(q) ? 0 : text.split(/[\s(),/-]+/).some((w) => w.startsWith(q)) ? 1 : 2;
}
function findProcedures(query) {
  const q = plain(query.trim());
  if (!q) return [...catalogue.values()];
  return [...catalogue.values()].map((p) => [rankText(haystack.get(p.id), q), p]).filter(([r]) => r >= 0)
    .sort((a, b) => a[0] - b[0]).map(([, p]) => p);
}

// A field that drops down a list (Saged, 2026-11-23): a tap shows every choice, typing narrows it, arrows and Enter
// pick. `options(q)` answers [{proc} | {label}]; `decorate(button, option)` adds what the caller needs; `onPick`
// runs on a pick (the done-today adder records through its own click handler instead).
let pickerCount = 0;
function picker(input, list, options, decorate, onPick) {
  const box = input.closest(".picker");
  list.id = list.id || `picker-${++pickerCount}`;
  input.setAttribute("aria-controls", list.id);
  let active = -1;
  const buttons = () => [...list.querySelectorAll("[role=option]")];
  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
  const mark = (i) => {
    const all = buttons();
    active = all.length ? (i + all.length) % all.length : -1;
    all.forEach((b, n) => b.classList.toggle("active", n === active));
    if (active >= 0) { all[active].scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", all[active].id); }
  };
  const open = () => {
    const found = options(input.value);
    list.replaceChildren(...found.map((o, n) => {
      const b = el("button", "picker-option");
      b.type = "button";
      b.id = `${list.id}-${n}`;
      b.setAttribute("role", "option");
      b.tabIndex = -1;
      const name = el("bdi", null, o.proc ? o.proc.name : o.label);
      name.dir = "auto";
      b.append(name);
      if (o.proc) {
        b.dataset.proc = o.proc.id;
        if (o.proc.price != null) b.append(el("span", "count num", moneyText(o.proc.price)));
      }
      if (decorate) decorate(b, o);
      b.addEventListener("mousedown", (event) => event.preventDefault());  // the field keeps the focus
      b.addEventListener("click", () => { close(); if (onPick) onPick(o); });
      return b;
    }));
    if (!found.length) list.replaceChildren(el("p", "muted picker-none", t("proc_none")));
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    active = -1;
  };
  input.addEventListener("focus", open);
  input.addEventListener("click", () => { if (list.hidden) open(); });
  input.addEventListener("input", () => { open(); if (input.value.trim()) mark(0); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) open();
      mark(active + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter") {
      if (list.hidden) return;
      event.preventDefault();
      const all = buttons();
      const b = all[active] || all[0];
      if (b) b.click();
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      close();
    }
  });
  box.addEventListener("focusout", (event) => { if (!box.contains(event.relatedTarget)) close(); });
  document.addEventListener("pointerdown", (event) => { if (!box.contains(event.target)) close(); });
  return { close };
}

function removeButton(name) {
  const b = el("button", "icon-btn danger-icon");
  b.type = "button";
  b.dataset.removeRecord = "";
  b.title = t("remove");
  b.setAttribute("aria-label", `${t("remove")}: ${name}`);
  b.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-x"/></svg>';
  return b;
}

function addProcedure(answer) {
  const p = answer.procedure;
  if (!p || procList.querySelector(`[data-record="${p.id}"]`)) return;
  const what = el("span", "proc-what");
  what.append(el("bdi", null, nameOf(p)));
  what.firstChild.dir = "auto";
  if (p.tooth) what.append(` · ${t("tooth_short", { n: localDigits(p.tooth) })}`);
  if (p.qty > 1) what.append(` × ${localDigits(p.qty)}`);
  const li = el("li");
  li.dataset.record = p.id;
  li.append(what);
  if (p.amount != null) li.append(el("span", "num", moneyText(p.amount)));  // none without money.view
  if (procList.hasAttribute("data-removable")) li.append(removeButton(nameOf(p)));
  procList.append(li);
  if (answer.invoice && procTotal) procTotal.textContent = moneyText(answer.invoice.total);
  offerRecipe(catalogue.get(p.procedure_id), p);
  // Its materials come into the visit's use card (Saged, 2026-11-25).
  window.dispatchEvent(new CustomEvent("hamada:use-add", { detail: { record: p.id, procedure_id: p.procedure_id, qty: p.qty || 1 } }));
}
window.addEventListener("hamada:procedure-recorded", (event) => addProcedure(event.detail));

// The X on a row (Saged, 2026-11-17): a procedure tapped by mistake goes, with its line, materials, plan step and
// tooth state, while the visit is on. Its key stays on the row until a definite answer, so a second tap or a resend
// never acts twice; the row's button is disabled while it is pending.
procList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-record]");
  const li = button && button.closest("[data-record]");
  if (!li || li.dataset.busy) return;
  const name = li.querySelector("bdi").textContent;
  const errorBox = document.querySelector("[data-add-procedure] [data-error]");
  const say = (text) => { if (errorBox) errorBox.textContent = text; else if (text) alertMessage(text); };
  li.dataset.busy = "1";
  button.disabled = true;
  say("");
  li.removeKey = li.removeKey || newKey();
  const result = await save("DELETE", `/api/v1/appointments/${id}/procedures/${li.dataset.record}`, {},
    { key: li.removeKey, label: `${t("remove")} · ${name}` });
  delete li.dataset.busy;
  button.disabled = false;
  if (!result.settled) { renderPending(); say(result.signedOut ? t("signed_out") : t("unknown")); return; }
  li.removeKey = null;
  if (!isOk(result) && result.status !== 404) { say(errorMessage(result, t("failed"))); return; }
  li.remove();  // a 404 is a record already gone (another tab, or an answer lost before)
  window.dispatchEvent(new CustomEvent("hamada:use-drop", { detail: { record: Number(li.dataset.record) } }));
  if (isOk(result)) {
    if (result.data.invoice && procTotal) procTotal.textContent = moneyText(result.data.invoice.total);
    window.dispatchEvent(new CustomEvent("hamada:procedure-removed", { detail: result.data }));  // the chart
  }
  toast(t("removed", { name }));
});

const adder = document.querySelector("[data-add-procedure]");
if (adder) {
  const form = adder.querySelector("form");
  const confirmBox = adder.querySelector("[data-proc-confirm]");
  const toothField = adder.querySelector("[data-tooth-field]");
  const toothInput = form.elements.namedItem("tooth");
  const search = adder.querySelector("[data-proc-search]");
  const results = adder.querySelector("[data-proc-results]");
  const cancel = adder.querySelector("[data-proc-cancel]");
  const countInput = adder.querySelector("[data-proc-count]");
  const count = () => {
    const n = Number(asciiDigits(countInput.value.trim()));
    return Number.isInteger(n) && n >= 1 && n <= 20 ? n : 1;
  };
  const submitter = new Submitter(adder, { label: t("record") });
  let chosen = null;

  function closeConfirm() {
    chosen = null;
    confirmBox.hidden = true;
    toothField.hidden = true;
    toothInput.value = "";
    cancel.hidden = false;
    adder.querySelectorAll("[data-run]").forEach((b) => { b.disabled = false; });
  }

  async function record(body) {
    const p = catalogue.get(body.procedure_id);
    const result = await submitter.run("POST", `/api/v1/appointments/${id}/procedures`, body, `${t("record")} · ${p ? p.name : ""}`);
    if (!result) {  // not confirmed: only "Try again" (the same key and body) or "Discard"
      adder.querySelector("[data-proc-name]").textContent = p ? p.name : "";
      confirmBox.hidden = false;
      toothField.hidden = true;
      cancel.hidden = true;
      return;
    }
    if (!isOk(result)) return;
    closeConfirm();
    if (body.plan_item_id) {  // the plan's session is done: its one-tap offer goes
      const offer = adder.querySelector(`[data-plan-item="${body.plan_item_id}"]`);
      if (offer) offer.remove();
      const box = adder.querySelector("[data-plan-offer]");
      if (box && !box.querySelector("[data-plan-item]")) box.hidden = true;
    }
    addProcedure(result.data);
    countInput.value = "1";
    toast(t("proc_added", { name: nameOf(result.data.procedure) }));
    search.value = "";
    results.replaceChildren();
  }

  function choose(p, planItem) {
    if (submitter.unknown || adder.dataset.busy) return;
    submitter.say("");
    // The procedure the plan set for this session: recorded against its plan item (which keeps its tooth and price).
    if (planItem) { record({ procedure_id: p.id, plan_item_id: planItem }); return; }
    if (p.unit !== "tooth") { record(count() > 1 ? { procedure_id: p.id, qty: count() } : { procedure_id: p.id }); return; }
    chosen = p;  // a tooth procedure: the number is optional, one more tap records it
    adder.querySelector("[data-proc-name]").textContent = p.name;
    confirmBox.hidden = false;
    toothField.hidden = false;
    toothInput.focus();
  }

  adder.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-proc]");
    if (chip && !chip.disabled) { const p = catalogue.get(Number(chip.dataset.proc)); if (p) choose(p, Number(chip.dataset.planItem) || null); }
  });
  cancel.addEventListener("click", closeConfirm);
  adder.querySelector("[data-discard]").addEventListener("click", closeConfirm);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitter.unknown) { record(submitter.unknown.body); return; }
    if (!chosen) return;
    const body = { procedure_id: chosen.id };
    const tooth = Number(asciiDigits(toothInput.value.trim()));
    if (toothInput.value.trim() && !tooth) { submitter.say(t("need_tooth")); return; }
    if (tooth) body.tooth = tooth;
    else if (count() > 1) body.qty = count();  // no tooth named: the count stands
    record(body);
  });

  // The whole price list drops down on a tap; the adder's click handler above records the one picked.
  picker(search, results, (q) => findProcedures(q).map((p) => ({ proc: p })), (b) => { b.dataset.run = ""; });
}

// --- "Suggest a discount": one tap suggests one of the clinic's discount types, a second tap clears it. ---
// It changes no money: the desk sees it in the Pay dialog and applies it. The request sets an absolute value and
// keeps its key until a definite answer; a different tap drops an unconfirmed one, so it is never resent later.

const suggestBox = document.querySelector("[data-suggest-discount]");
if (suggestBox) {
  const suggestError = suggestBox.querySelector("[data-suggest-error]");
  let held = null;  // {key, typeId} whose answer was lost
  suggestBox.addEventListener("click", async (event) => {
    const chip = event.target.closest("[data-type]");
    if (!chip || suggestBox.dataset.busy) return;
    const typeId = chip.getAttribute("aria-pressed") === "true" ? null : Number(chip.dataset.type);
    if (held && held.typeId !== typeId) { discardPending(held.key); held = null; }
    const request = held || { key: newKey(), typeId };
    suggestBox.dataset.busy = "1";
    suggestError.textContent = "";
    const result = await save("PUT", `/api/v1/appointments/${id}/suggested-discount`, { type_id: typeId },
      { key: request.key, label: `${t("suggest_discount")} · ${chip.querySelector("bdi").textContent}` });
    delete suggestBox.dataset.busy;
    if (!result.settled) {
      held = request;
      renderPending();
      if (!result.signedOut) suggestError.textContent = t("unknown");
      return;
    }
    held = null;
    if (!isOk(result)) { suggestError.textContent = errorMessage(result, t("failed")); return; }
    const now = result.data.suggested;
    suggestBox.querySelectorAll("[data-type]").forEach((b) => {
      b.setAttribute("aria-pressed", String(Boolean(now) && Number(b.dataset.type) === now.id));
    });
    toast(now ? t("discount_suggested", { name: now.name }) : t("discount_unsuggested"));
  });
}

// --- "What comes next": the follow-ups. The desk's list shows them once the visit is finished. ---

const nextList = document.querySelector("[data-upcoming]");
const nextEmpty = document.querySelector("[data-no-upcoming]");
const PRESETS = [[2, "in_2days"], [7, "in_week"], [14, "in_2weeks"], [30, "in_month"]];

function afterText(days) {
  const preset = PRESETS.find(([n]) => n === days);
  if (preset) return t(preset[1]);
  if (days > 0 && days % 30 === 0) return t("in_months", { n: localDigits(days / 30) });
  if (days > 0 && days % 7 === 0) return t("in_weeks", { n: localDigits(days / 7) });
  return days > 0 ? t("in_days", { n: localDigits(days) }) : "";
}
function dayText(iso) {
  const [, m, d] = iso.split("-").map(Number);
  return localDigits(`${d}/${m}`);
}

// One follow-up action: its key stays until a definite answer, so a second tap or a resend never acts twice.
async function followUp(holder, url, body, label) {
  if (holder.dataset.busy) return null;
  holder.dataset.busy = "1";
  holder.followKey = holder.followKey || newKey();
  const result = await save("POST", url, body, { key: holder.followKey, label });
  delete holder.dataset.busy;
  if (!result.settled) { renderPending(); toast(t("unknown")); return null; }
  holder.followKey = null;
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return null; }
  renderUpcoming(result.data.upcoming);
  return result;
}

function renderUpcoming(items) {
  if (!nextList) return;
  nextList.replaceChildren(...items.map((item) => {
    const li = el("li");
    li.dataset.item = item.id;
    const what = el("span");
    const name = el("bdi", null, item.name);
    name.dir = "auto";
    what.append(name);
    if (item.tooth) what.append(` · ${t("tooth_short", { n: localDigits(item.tooth) })}`);
    const when = [afterText(item.days), dayText(item.due_on)].filter(Boolean).join(" · ");
    const side = el("span", "next-side");
    if (item.booked) { side.append(el("span", "pill teal", `${t("fu_booked")} ${dayText(item.booked_day)}`)); li.append(what, side); return li; }
    if (item.visit_id !== id) { side.append(el("span", "num", when)); li.append(what, side); return li; }  // an earlier visit's
    const change = el("button", "chip-btn", when);
    change.type = "button";
    change.setAttribute("aria-expanded", "false");
    change.setAttribute("aria-label", `${t("fu_change")}: ${item.name}, ${when}`);
    const remove = el("button", "icon-btn", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("remove")}: ${item.name}`);
    remove.addEventListener("click", () => followUp(li, `/api/v1/follow-ups/${item.id}/drop`, {}, `${t("remove")} · ${item.name}`));
    change.addEventListener("click", () => {
      const open = li.querySelector(".next-when");
      if (open) { open.remove(); change.setAttribute("aria-expanded", "false"); return; }
      const row = el("span", "chips next-when");
      PRESETS.forEach(([days, key]) => {
        const b = el("button", "chip-btn", t(key));
        b.type = "button";
        b.dataset.days = days;
        b.addEventListener("click", () => followUp(li, `/api/v1/follow-ups/${item.id}/due`, { days }, `${item.name} · ${t(key)}`));
        row.append(b);
      });
      li.append(row);
      change.setAttribute("aria-expanded", "true");
    });
    side.append(change, remove);
    li.append(what, side);
    return li;
  }));
  nextEmpty.hidden = items.length > 0;
  const send = document.querySelector("[data-next-send]");  // the plan by WhatsApp: only with something to send
  if (send) send.hidden = !items.length;
  window.dispatchEvent(new CustomEvent("hamada:upcoming"));  // "Make a plan" or "Add to the plan"
}
if (nextList) renderUpcoming(data.upcoming || []);

// A recorded procedure with a recipe offers its sessions: "Two sessions: in a week and in 2 weeks. Add them?"
function offerRecipe(p, recorded) {
  if (!p || !offers || !nextList || !p.follow_up.length || offers.querySelector(`[data-offer="${recorded.id}"]`)) return;
  const n = p.follow_up.length;
  const count = n === 1 ? t("fu_count_1") : n === 2 ? t("fu_count_2") : t("fu_count_n", { n: localDigits(n) });
  const when = p.follow_up.map((s) => afterText(s.days)).reduce((a, b) => t("fu_pair", { a, b }));
  const box = el("div", "notice offer");
  box.dataset.offer = recorded.id;
  box.setAttribute("role", "group");
  const text = el("p", null, t("fu_offer", { name: p.name, count, when }));
  const add = el("button", "btn", t("fu_add"));
  add.type = "button";
  add.dataset.offerAdd = "";
  const skip = el("button", "btn ghost", t("fu_skip"));
  skip.type = "button";
  const actions = el("div", "appt-actions");
  actions.append(add, skip);
  box.append(text, actions);
  add.addEventListener("click", async () => {
    const body = { recipe_of: p.id };
    if (recorded.tooth) body.tooth = recorded.tooth;
    add.disabled = true;
    const result = await followUp(box, `/api/v1/appointments/${id}/follow-ups`, body, `${t("fu_add")} · ${p.name}`);
    add.disabled = false;
    if (!result) return;
    box.remove();
    toast(t("fu_added"));
  });
  skip.addEventListener("click", () => box.remove());
  offers.append(box);
}

const nextAdd = document.querySelector("[data-next-add]");
if (nextAdd) {
  const form = nextAdd.querySelector("[data-plan-form]");
  const start = nextAdd.querySelector("[data-plan-start]");
  const rowsBox = form.querySelector("[data-plan-rows]");
  const gaps = form.querySelector("[data-plan-gaps]");
  const saveButton = form.querySelector("[data-plan-save]");
  const error = form.querySelector("[data-error]");
  const rowTemplate = nextAdd.querySelector("[data-plan-row]");
  const adding = new Submitter(nextAdd, { label: t("fu_title") });
  const retry = form.querySelector("[data-submit]");
  const fixed = [form.dataset.followLabel, form.dataset.continueLabel];
  const dateFormat = new Intl.DateTimeFormat(document.documentElement.lang === "en" ? "en-GB" : "ar-EG-u-nu-latn",
    { weekday: "long", day: "numeric", month: "long" });
  let gap = { n: 1, unit: 7 };

  const startText = () => { start.textContent = nextList && nextList.children.length ? t("plan_more") : t("plan_make"); };
  startText();
  const rows = () => [...rowsBox.children];
  const rowDays = (li) => {
    const n = Number(asciiDigits(li.querySelector("[data-plan-n]").value.trim()));
    return Number.isInteger(n) && n >= 1 ? n * Number(li.querySelector("[data-plan-unit]").value) : null;
  };
  // Each row's date: the days add up from this visit, session after session.
  function dates() {
    let total = 0;
    rows().forEach((li, i) => {
      li.querySelector(".plan-n").textContent = t("plan_session", { n: localDigits(i + 1) });
      li.querySelector("[data-after-first]").hidden = i > 0;
      li.querySelector("[data-after-prev]").hidden = i === 0;
      const days = rowDays(li);
      const out = li.querySelector("[data-plan-date]");
      if (days === null || total === null) { total = null; out.textContent = ""; return; }
      total += days;
      const day = new Date();
      day.setDate(day.getDate() + total);
      out.textContent = `← ${localDigits(dateFormat.format(day))}`;
    });
  }
  function newRow(copyFrom) {
    const li = rowTemplate.content.firstElementChild.cloneNode(true);
    const what = li.querySelector("[data-plan-what]");
    const n = li.querySelector("[data-plan-n]");
    const unit = li.querySelector("[data-plan-unit]");
    n.value = gap.n;
    unit.value = String(gap.unit);
    if (copyFrom) {  // a new session is what the one before was, at the same gap
      const was = copyFrom.querySelector("[data-plan-what]");
      what.value = was.value;
      if (was.dataset.proc) what.dataset.proc = was.dataset.proc;
      n.value = copyFrom.querySelector("[data-plan-n]").value;
      unit.value = copyFrom.querySelector("[data-plan-unit]").value;
    }
    const list = li.querySelector(".picker-list");
    what.id = `plan-what-${++pickerCount}`;
    li.querySelector(".plan-what label").htmlFor = what.id;
    picker(what, list, (q) => [
      ...fixed.filter((label) => !plain(q.trim()) || plain(label).includes(plain(q.trim()))).map((label) => ({ label })),
      ...findProcedures(q).map((p) => ({ proc: p })),
    ], null, (o) => {
      what.value = o.proc ? o.proc.name : o.label;
      what.closest(".picker").classList.remove("invalid");
      if (o.proc) what.dataset.proc = o.proc.id; else delete what.dataset.proc;
    });
    what.addEventListener("input", () => { delete what.dataset.proc; what.closest(".picker").classList.remove("invalid"); });
    n.addEventListener("input", dates);
    unit.addEventListener("change", dates);
    return li;
  }
  function setCount(count) {
    form.querySelectorAll("[data-plan-count] [data-n]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.n) === count)));
    while (rows().length > count) rowsBox.lastElementChild.remove();
    while (rows().length < count) rowsBox.append(newRow(rowsBox.lastElementChild));
    gaps.hidden = false;
    saveButton.hidden = Boolean(adding.unknown);
    error.textContent = "";
    dates();
  }
  function reset() {
    rowsBox.replaceChildren();
    form.querySelectorAll("[data-plan-count] [data-n]").forEach((b) => b.setAttribute("aria-pressed", "false"));
    gaps.hidden = true;
    saveButton.hidden = true;
    error.textContent = "";
  }
  const settle = () => {
    retry.hidden = !adding.unknown;
    saveButton.hidden = Boolean(adding.unknown) || !rows().length;
  };

  start.addEventListener("click", () => {
    reset();
    form.hidden = false;
    start.hidden = true;
    form.querySelector("[data-plan-count] [data-n]").focus();
  });
  form.querySelector("[data-plan-cancel]").addEventListener("click", () => {
    if (adding.unknown) return;  // an unconfirmed plan: "Try again" or "Discard" first
    reset();
    form.hidden = true;
    start.hidden = false;
    start.focus();
  });
  form.querySelector("[data-plan-count]").addEventListener("click", (event) => {
    const b = event.target.closest("[data-n]");
    if (b && !adding.unknown) setCount(Number(b.dataset.n));
  });
  form.querySelector("[data-plan-gap]").addEventListener("click", (event) => {
    const b = event.target.closest("[data-n]");
    if (!b || adding.unknown) return;
    gap = { n: Number(b.dataset.n), unit: Number(b.dataset.unit) };
    form.querySelectorAll("[data-plan-gap] [data-n]").forEach((c) => c.setAttribute("aria-pressed", String(c === b)));
    rows().forEach((li) => { li.querySelector("[data-plan-n]").value = gap.n; li.querySelector("[data-plan-unit]").value = String(gap.unit); });
    dates();
  });

  function items() {
    let total = 0;
    const out = [];
    for (const li of rows()) {
      const days = rowDays(li);
      if (days === null) { error.textContent = t("fu_bad_days"); li.querySelector("[data-plan-n]").focus(); return null; }
      total += days;
      if (total > 730) { error.textContent = t("plan_too_far"); li.querySelector("[data-plan-n]").focus(); return null; }
      const what = li.querySelector("[data-plan-what]");
      const typed = what.value.trim();
      if (!typed) {  // every session says what it is (Saged, 2026-11-23): an empty one is never saved as a guess
        what.closest(".picker").classList.add("invalid");
        error.textContent = t("plan_need_what");
        what.focus();
        return null;
      }
      const proc = what.dataset.proc && catalogue.get(Number(what.dataset.proc));
      if (proc && proc.name === typed) out.push({ procedure_id: proc.id, days: total });
      else out.push({ label: typed, days: total });
    }
    return out;
  }
  async function send(body) {
    const result = await adding.run("POST", `/api/v1/appointments/${id}/follow-ups`, body);
    settle();
    if (!result || !isOk(result)) return;
    renderUpcoming(result.data.upcoming);
    reset();
    form.hidden = true;
    start.hidden = false;
    startText();
    toast(t("plan_saved"));
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (adding.unknown) { send(adding.unknown.body); return; }
    error.textContent = "";
    const list = items();
    if (list && list.length) send({ items: list });
  });
  form.querySelector("[data-discard]").addEventListener("click", settle);
  window.addEventListener("hamada:upcoming", startText);
}

// --- the dental chart, loaded when first opened (the teeth page's part and script) ---

const fold = document.querySelector("[data-chart-fold]");
if (fold) {
  let loaded = false;
  fold.addEventListener("toggle", async () => {
    if (!fold.open || loaded) return;
    loaded = true;
    const status = fold.querySelector("[data-chart-status]");
    status.textContent = t("loading");
    const result = await fetchText(fold.dataset.chartUrl);
    if (result.status !== 200) {
      loaded = false;
      if (result.status === 401) { window.dispatchEvent(new CustomEvent("hamada:signed-out")); return; }
      status.textContent = errorMessage(result, t("chart_failed"));
      return;
    }
    fold.querySelector("[data-chart-slot]").replaceChildren(parseFragment(result.text));
    await import(fold.dataset.teethJs);
  });
}

// --- a prescription: ready prescriptions and the section's medicines ---

const rxDialog = document.getElementById("rx-dialog");
if (rxDialog) {
  const form = rxDialog.querySelector("form");
  const lines = rxDialog.querySelector("[data-rx-lines]");
  const picks = rxDialog.querySelector("[data-rx-templates]");
  const chosen = rxDialog.querySelector("[data-rx-chosen]");
  const list = rxDialog.querySelector("[data-rx-medicines]");
  const writing = new Submitter(rxDialog, { label: t("rx_number", { n: "" }) });
  let medicines = [];
  let templates = [];
  let library = false;
  wireClose(rxDialog);

  function addLine(line, fromRx = null) {
    const li = el("li", "rx-write-line");
    li.line = line;
    if (fromRx != null) li.dataset.fromRx = String(fromRx);
    const name = el("bdi", "rx-med", [line.name, line.strength].filter(Boolean).join(" "));
    name.dir = "auto";
    const dose = el("input");
    dose.name = "dose";
    dose.value = line.dose || "";
    dose.maxLength = 200;
    dose.dir = "auto";
    dose.autocomplete = "off";
    dose.setAttribute("aria-label", `${t("rx_dose")}: ${line.name}`);
    const remove = el("button", "icon-btn", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("remove")}: ${line.name}`);
    remove.addEventListener("click", () => { li.remove(); markPicks(); });
    li.append(name, dose, remove);
    lines.append(li);
  }

  // A field that drops a list under it (Saged, 2026-11-25): a tap opens it, typing filters it, arrows and Enter pick.
  // Our own list, not the browser's <datalist>, whose drop-down froze on Windows (2026-11-19).
  function combo(input, listEl, options, onEnter) {
    let active = -1;
    let shown = [];
    const close = () => {
      listEl.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };
    const mark = () => {
      [...listEl.children].forEach((li, i) => li.classList.toggle("active", i === active));
      const li = listEl.children[active];
      if (li) { input.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
      else input.removeAttribute("aria-activedescendant");
    };
    const choose = (item) => { if (rxDialog.dataset.busy || writing.unknown) return; item.pick(); };
    function open() {
      shown = options(input.value);
      active = -1;
      listEl.replaceChildren(...shown.map((item, i) => {
        const li = el("li", item.on ? "on" : null);
        li.id = `${listEl.id}-${i}`;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", item.on ? "true" : "false");
        const name = el("bdi", null, item.label);
        name.dir = "auto";
        li.append(name);
        if (item.on) { const tick = el("span", "combo-tick", "✓"); tick.setAttribute("aria-hidden", "true"); li.append(tick); }
        li.addEventListener("mousedown", (event) => event.preventDefault());  // the field keeps the focus
        li.addEventListener("click", () => choose(item));
        return li;
      }));
      listEl.hidden = !shown.length;
      input.setAttribute("aria-expanded", shown.length ? "true" : "false");
    }
    input.addEventListener("focus", open);
    input.addEventListener("click", () => { if (listEl.hidden) open(); });
    input.addEventListener("input", open);
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (listEl.hidden) { open(); return; }
        if (!shown.length) return;
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
        mark();
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (active >= 0 && !listEl.hidden) choose(shown[active]); else if (onEnter) onEnter();
      } else if (event.key === "Escape" && !listEl.hidden) {
        event.preventDefault();   // closes the list, not the sheet
        event.stopPropagation();
        close();
      }
    });
    return { open, close };
  }

  const plainText = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
    .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");

  // A ready prescription is one pick on, one pick off (Saged, 2026-11-24): its medicines come in together and go out
  // together, it is never added twice; the chosen ones show under the field, each with an x to take it out.
  const linesOf = (rxId) => [...lines.children].filter((li) => li.dataset.fromRx === String(rxId));
  function markPicks() {
    chosen.replaceChildren(...templates.filter((r) => linesOf(r.id).length).map((r) => {
      const b = el("button", "chip-btn");
      b.type = "button";
      b.dataset.run = "";
      b.setAttribute("aria-pressed", "true");
      b.setAttribute("aria-label", `${t("remove")}: ${r.name}`);
      const name = el("bdi", null, r.name);
      name.dir = "auto";
      b.append(name, " ×");
      b.addEventListener("click", () => takeOut(r));
      return b;
    }));
  }
  function takeOut(r) {
    linesOf(r.id).forEach((li) => li.remove());
    const notes = form.elements.namedItem("notes");
    if (r.notes && notes.value === r.notes) notes.value = "";
    markPicks();
  }
  function putIn(r) {
    r.items.forEach((item) => addLine({ medicine_id: item.medicine_id, name: item.name, strength: item.strength,
                                        dose: item.dose, note: item.note }, r.id));
    const notes = form.elements.namedItem("notes");
    if (r.notes && !notes.value) notes.value = r.notes;
    markPicks();
  }

  const readyInput = form.elements.namedItem("ready");
  readyInput.addEventListener("blur", () => { readyInput.value = ""; });   // it only filters the list
  const ready = combo(readyInput, picks, (typed) => {
    const q = plainText(typed.trim());
    return templates.filter((r) => !q || plainText(r.name).includes(q)).map((r) => ({
      label: r.name,
      on: linesOf(r.id).length > 0,
      pick: () => { if (linesOf(r.id).length) takeOut(r); else putIn(r); readyInput.value = ""; ready.open(); },
    }));
  });

  async function loadLibrary() {
    if (library) return;
    const section = data.section_id ? `?section=${data.section_id}` : "";
    const [rx, meds] = await Promise.all([get(`/api/v1/prescription-templates${section}`), get(`/api/v1/medicines${section}`)]);
    if (!isOk(rx) || !isOk(meds)) { writing.say(errorMessage(isOk(rx) ? meds : rx, t("failed"))); return; }
    library = true;
    medicines = meds.data.items;
    templates = rx.data.items;
    readyInput.closest(".combo").hidden = !templates.length;
    markPicks();
  }

  // Medicines: a tap lists the section's medicines; typing filters from one letter, by name or active ingredient,
  // spelling variants and Arabic letters for a Latin name match; a word that starts with what was typed comes first.
  const medInput = form.elements.namedItem("medicine");
  const lineOf = (m) => ({ medicine_id: m.id, name: m.name, strength: m.strength, dose: m.default_dose, note: m.default_note });
  function addTyped(m) {
    const typed = medInput.value.trim();
    if (!m && !typed) return;
    m = m || medicines.find((x) => plainText(x.name) === plainText(typed));
    addLine(m ? lineOf(m) : { name: typed });
    medInput.value = "";
    meds.close();
    medInput.focus();
  }
  const meds = combo(medInput, list, (typed) => {
    const q = plainText(typed.trim());
    const rank = (m) => {
      if (!q) return 0;
      const text = plainText(`${m.name} ${m.generic || ""}`);
      // One letter looks at the names only, so "ب" lists Brufen and Panadol, not every ingredient with a B in it.
      if (!text.includes(q)) {
        return soundsLike(typed, m.name) ? 3 : q.length > 1 && soundsLike(typed, m.generic) ? 4 : -1;
      }
      return text.startsWith(q) ? 0 : text.split(/[\s(),/-]+/).some((w) => w.startsWith(q)) ? 1 : 2;
    };
    return medicines.map((m) => [rank(m), m]).filter(([r]) => r >= 0).sort((a, b) => a[0] - b[0])
      .map(([, m]) => ({ label: [m.name, m.strength].filter(Boolean).join(" "), pick: () => addTyped(m) }));
  }, () => addTyped(null));
  rxDialog.querySelector("[data-rx-add]").addEventListener("click", () => addTyped(null));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let body = writing.unknown ? writing.unknown.body : null;
    if (!body) {
      const items = Array.from(lines.children, (li) => {
        const line = { name: li.line.name, dose: li.querySelector('[name="dose"]').value.trim(), note: li.line.note || "" };
        if (li.line.medicine_id) line.medicine_id = li.line.medicine_id;
        if (li.line.strength) line.strength = li.line.strength;
        return line;
      });
      if (!items.length) { writing.say(t("rx_need_line")); return; }
      body = { patient_id: data.patient_id, appointment_id: id, notes: form.elements.namedItem("notes").value.trim(), items };
    }
    const result = await writing.run("POST", "/api/v1/prescriptions", body);
    if (!result || !isOk(result)) return;
    const rx = result.data.prescription;
    rxDialog.close();
    const li = el("li");
    li.append(el("span", null, t("rx_number", { n: localDigits(rx.number) })));
    document.querySelector("[data-rx-list]").append(li);
    toast(t("rx_written", { n: localDigits(rx.number) }));
    if (rx.warnings && rx.warnings.length) {
      alertMessage(rx.warnings.map((w) => t("rx_allergy", { name: w.name, allergy: w.allergy })).join(" · "));
    }
  });

  document.querySelector("[data-open-rx]").addEventListener("click", (event) => {
    if (!writing.holdsUnknown()) { form.reset(); lines.replaceChildren(); writing.reset(); markPicks(); }
    openDialog(rxDialog, event.currentTarget);
    loadLibrary();
  });
}

// --- "Consultation only" (Saged, 2026-11-24): the patient was seen and nothing else was done ---
const consultBox = document.querySelector("[data-consult-only]");
if (consultBox) {
  // A procedure on the visit means it isn't a consultation only (Saged, 2026-11-25): the box is cleared and locked
  // while any is recorded (the server clears it too), and comes back when the last one is removed.
  const hasProcedure = () => Boolean(procList && procList.querySelector("[data-record]"));
  const lockConsult = () => {
    const done = hasProcedure();
    if (done) consultBox.checked = false;
    consultBox.disabled = done;
    consultBox.closest("label").classList.toggle("off", done);
  };
  lockConsult();
  if (procList) new MutationObserver(lockConsult).observe(procList, { childList: true });
  consultBox.addEventListener("change", async () => {
    const on = consultBox.checked;
    consultBox.disabled = true;
    const result = await save("POST", `/api/v1/appointments/${id}/consult-only`, { on }, { label: t("consult_only") });
    lockConsult();
    if (result.settled && isOk(result)) return;
    if (result.settled) { consultBox.checked = !on; alertMessage(errorMessage(result, t("failed"))); }
  });
}

// --- "Finish the visit": the notes first, then the finish; Today shows the next patient ---

const finishButton = document.querySelector("[data-finish]");
if (finishButton) {
  const finishError = document.querySelector("[data-finish-error]");
  finishButton.addEventListener("click", async () => {
    if (finishButton.disabled) return;
    finishButton.disabled = true;
    finishError.textContent = "";
    try {
      if (box.value !== saved && !(await flush())) { finishError.textContent = t("note_unsaved"); return; }
      if (!(await flushMedical())) { finishError.textContent = t("medical_unsaved"); return; }
      // The least a visit holds before it ends (Saged, 2026-11-24): what was done, and notes or the chart.
      const consultOnly = document.querySelector("[data-consult-only]");
      if (!(consultOnly && consultOnly.checked) && !(procList && procList.querySelector("[data-record]"))) {
        finishError.textContent = t("finish_need_done");
        return;
      }
      if (!box.value.trim() && !charted) { finishError.textContent = t("finish_need_notes"); box.focus(); return; }
      const body = { status: "in_room", check_record: true };
      // What the use card holds comes off the stock with the finish (Saged, 2026-11-25).
      const useCard = document.querySelector("[data-use-card]");
      if (useCard && useCard.useLines) {
        const uses = useCard.useLines();
        if (uses.error) { finishError.textContent = uses.error; return; }
        body.uses = uses.lines;
        body.records = uses.records;
      }
      const result = await save("POST", `/api/v1/appointments/${id}/finish`, body,
        { label: t("finish"), auto: true });  // carries the status it expects: a resend is harmless
      if (!result.settled) { renderPending(); finishError.textContent = t("finish_unknown"); return; }
      const code = result.data && result.data.error && result.data.error.code;
      if (isOk(result) || code === "status_changed") {  // done, or no longer in the room: Today shows where it is
        leaving = true;
        try {
          sessionStorage.removeItem(draftKey);
          if (isOk(result) && useCard && useCard.clearUses) useCard.clearUses();
          // "Back to the visit" on Today for five minutes, the timer still running (Saged, 2026-11-24).
          sessionStorage.setItem("hamada.finished", JSON.stringify({ id, name: data.patient_name, called_at: data.called_at,
            finished_at: Date.now() + skew, skew }));
        } catch (e) { /* none */ }
        window.location.assign("/");
        return;
      }
      finishError.textContent = errorMessage(result, t("failed"));
    } finally {
      if (!leaving) finishButton.disabled = false;
    }
  });
}

// --- the patient file in a window over the visit, not a new tab (Saged, 2026-11-22) ---
const fileDialog = document.getElementById("file-dialog");
if (fileDialog) {
  const frame = fileDialog.querySelector("[data-file-frame]");
  wireClose(fileDialog);
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-open-file]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;  // a new tab if asked for
    event.preventDefault();
    const url = `${link.getAttribute("href")}?embed=1`;
    if (frame.dataset.src !== url) { frame.dataset.src = url; frame.src = url; }
    else frame.contentWindow.location.reload();  // fresh each time it opens
    openDialog(fileDialog, link);
  });
}

// --- What the visit really is (Saged, 2026-11-25): the doctor changes the booked service; the bill follows ---
const serviceBox = document.querySelector("[data-change-service]");
if (serviceBox) {
  const button = serviceBox.querySelector("[data-save-service]");
  const error = serviceBox.querySelector("[data-service-error]");
  button.addEventListener("click", async () => {
    const serviceId = Number(serviceBox.querySelector('[name="service_id"]').value);
    button.disabled = true;
    error.textContent = "";
    const result = await save("POST", `/api/v1/appointments/${id}/service`, { service_id: serviceId }, { label: t("service_changed") });
    button.disabled = false;
    if (isOk(result)) { toast(t("service_changed")); window.location.reload(); return; }
    error.textContent = errorMessage(result, t("failed"));
  });
}
