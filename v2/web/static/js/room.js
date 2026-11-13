// The doctor's visit screen (Saged, 2026-11-13): the timer, the notes saved as the doctor types, procedures,
// a prescription, the dental chart on demand, and "Finish the visit", the one way out.
// - Notes: PUT appointments/{id}/note with If-Match and a request key; the same text keeps its key until a
//   definite answer, so a resend after a lost answer never saves twice. The text waits in sessionStorage
//   (this tab only, cleared once saved) so a reload or a sign-in never loses it.
// - Leaving the page warns, except after the finish, a sign-out or a language switch.
import { errorMessage, fetchText, get, isOk, membership, newKey, save, send } from "./api.js";
import { Submitter, el, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { asciiDigits, localDigits, moneyText } from "./numbers.js";
import { alertMessage, renderPending, toast } from "./shell.js";
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
window.addEventListener("online", () => { if (box.value !== saved) flush(); });
if (box.value !== saved) flush(); else if (saved) say(t("note_saved"));

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

// --- procedures this visit ---

const procList = document.querySelector("[data-procedures]");
const procEmpty = document.querySelector("[data-no-procedures]");
const procTotal = document.querySelector("[data-procs-total]");

function addProcedure(answer) {
  const p = answer.procedure;
  if (!p || procList.querySelector(`[data-record="${p.id}"]`)) return;
  const what = el("span");
  what.append(el("bdi", null, nameOf(p)));
  what.firstChild.dir = "auto";
  if (p.tooth) what.append(` · ${t("tooth_short", { n: localDigits(p.tooth) })}`);
  if (p.qty > 1) what.append(` × ${localDigits(p.qty)}`);
  const li = el("li");
  li.dataset.record = p.id;
  li.append(what, el("span", "num", moneyText(p.amount)));
  procList.append(li);
  procEmpty.hidden = true;
  if (answer.invoice) procTotal.textContent = moneyText(answer.invoice.total);
}
window.addEventListener("hamada:procedure-recorded", (event) => addProcedure(event.detail));

const adder = document.querySelector("[data-add-procedure]");
if (adder) {
  const form = adder.querySelector("form");
  const select = form.elements.namedItem("procedure_id");
  const toothField = adder.querySelector("[data-tooth-field]");
  const submitter = new Submitter(adder, { label: t("record") });
  const unit = () => (select.selectedOptions[0] ? select.selectedOptions[0].dataset.unit : "");
  select.addEventListener("change", () => { toothField.hidden = unit() !== "tooth"; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let body = submitter.unknown ? submitter.unknown.body : null;
    if (!body) {
      const procedure = Number(select.value);
      if (!procedure) { submitter.say(t("need_procedure")); return; }
      body = { procedure_id: procedure };
      if (unit() === "tooth") {
        const tooth = Number(asciiDigits(form.elements.namedItem("tooth").value.trim()));
        if (!tooth) { submitter.say(t("need_tooth")); return; }
        body.tooth = tooth;
      }
    }
    const name = (select.querySelector(`option[value="${body.procedure_id}"]`) || {}).textContent || "";
    const result = await submitter.run("POST", `/api/v1/appointments/${id}/procedures`, body, `${t("record")} · ${name}`);
    if (!result || !isOk(result)) return;
    addProcedure(result.data);
    toast(t("proc_added", { name: nameOf(result.data.procedure) }));
    form.reset();
    toothField.hidden = true;
  });
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
  const list = rxDialog.querySelector("[data-rx-medicines]");
  const writing = new Submitter(rxDialog, { label: t("rx_number", { n: "" }) });
  let medicines = [];
  let library = false;
  wireClose(rxDialog);

  function addLine(line) {
    const li = el("li", "rx-write-line");
    li.line = line;
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
    remove.addEventListener("click", () => li.remove());
    li.append(name, dose, remove);
    lines.append(li);
  }

  async function loadLibrary() {
    if (library) return;
    const section = data.section_id ? `?section=${data.section_id}` : "";
    const [rx, meds] = await Promise.all([get(`/api/v1/prescription-templates${section}`), get(`/api/v1/medicines${section}`)]);
    if (!isOk(rx) || !isOk(meds)) { writing.say(errorMessage(isOk(rx) ? meds : rx, t("failed"))); return; }
    library = true;
    medicines = meds.data.items;
    list.replaceChildren(...medicines.map((m) => { const o = el("option"); o.value = m.name; return o; }));
    picks.replaceChildren(...rx.data.items.map((r) => {
      const b = el("button", "btn ghost", r.name);
      b.type = "button";
      b.dataset.run = "";
      b.addEventListener("click", () => {
        r.items.forEach((item) => addLine({ medicine_id: item.medicine_id, name: item.name, strength: item.strength,
                                            dose: item.dose, note: item.note }));
        if (r.notes && !form.elements.namedItem("notes").value) form.elements.namedItem("notes").value = r.notes;
      });
      return b;
    }));
  }

  rxDialog.querySelector("[data-rx-add]").addEventListener("click", () => {
    const input = form.elements.namedItem("medicine");
    const typed = input.value.trim();
    if (!typed) return;
    const m = medicines.find((x) => x.name.toLocaleLowerCase() === typed.toLocaleLowerCase());
    addLine(m ? { medicine_id: m.id, name: m.name, strength: m.strength, dose: m.default_dose, note: m.default_note }
      : { name: typed });
    input.value = "";
    input.focus();
  });
  form.elements.namedItem("medicine").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); rxDialog.querySelector("[data-rx-add]").click(); }
  });

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
    if (!writing.holdsUnknown()) { form.reset(); lines.replaceChildren(); writing.reset(); }
    openDialog(rxDialog, event.currentTarget);
    loadLibrary();
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
      const result = await save("POST", `/api/v1/appointments/${id}/finish`, { status: "in_room" },
        { label: t("finish"), auto: true });  // carries the status it expects: a resend is harmless
      if (!result.settled) { renderPending(); finishError.textContent = t("finish_unknown"); return; }
      const code = result.data && result.data.error && result.data.error.code;
      if (isOk(result) || code === "status_changed") {  // done, or no longer in the room: Today shows where it is
        leaving = true;
        try { sessionStorage.removeItem(draftKey); } catch (e) { /* none */ }
        window.location.assign("/");
        return;
      }
      finishError.textContent = errorMessage(result, t("failed"));
    } finally {
      if (!leaving) finishButton.disabled = false;
    }
  });
}
