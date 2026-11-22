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

// --- "Done today": the small bill. The six most used procedures are one tap; the rest are found by typing. ---
// The price is the clinic's list price: the screen never sends one, and the server refuses any other (price_locked).

const procList = document.querySelector("[data-procedures]");
const procTotal = document.querySelector("[data-procs-total]");
const offers = document.querySelector("[data-offers]");
const catalogue = new Map((data.procedures || []).map((p) => [p.id, p]));

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

  // The search (Saged, 2026-11-17): from two letters, Arabic or English, either name or the section's name
  // ("تج" finds the cosmetic ones). Spelling variants match (أ إ آ ا, ة ه, ى ي); a word that starts with what was
  // typed comes first. The quick chips above are searched too.
  const plain = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
    .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");
  const haystack = new Map([...catalogue.values()].map((p) => [p.id, plain(`${p.name} ${p.find || ""}`)]));
  search.addEventListener("input", () => {
    const q = plain(search.value.trim());
    const rank = (p) => {
      const text = haystack.get(p.id);
      if (!text.includes(q)) return -1;
      return text.startsWith(q) ? 0 : text.split(/[\s(),/-]+/).some((w) => w.startsWith(q)) ? 1 : 2;
    };
    const found = q.length >= 2 ? [...catalogue.values()].map((p) => [rank(p), p]).filter(([r]) => r >= 0)
      .sort((a, b) => a[0] - b[0]).slice(0, 12).map(([, p]) => p) : [];
    results.replaceChildren(...found.map((p) => {
      const b = el("button", "chip-btn");
      b.type = "button";
      b.dataset.run = "";
      b.dataset.proc = p.id;
      const name = el("bdi", null, p.name);
      name.dir = "auto";
      b.append(name);
      if (p.price != null) b.append(" ", el("span", "count num", moneyText(p.price)));
      return b;
    }));
    if (q.length >= 2 && !found.length) results.replaceChildren(el("span", "muted", t("proc_none")));
  });
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const first = results.querySelector("[data-proc]");
    if (first) first.click();
  });
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
  const form = nextAdd.querySelector("form");
  const adding = new Submitter(nextAdd, { label: t("fu_title") });
  const retry = nextAdd.querySelector("[data-submit]");
  const settle = () => {
    retry.hidden = !adding.unknown;
    nextAdd.querySelectorAll("[data-run]").forEach((b) => { b.disabled = Boolean(adding.unknown); });
  };

  async function addNext(body) {
    const result = await adding.run("POST", `/api/v1/appointments/${id}/follow-ups`, body);
    settle();
    if (!result || !isOk(result)) return;
    renderUpcoming(result.data.upcoming);
    form.elements.namedItem("other").value = "";
    toast(t("fu_added"));
  }
  nextAdd.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-next-when] [data-days], [data-days-custom]");
    if (!chip || chip.disabled || adding.unknown) return;
    let days = Number(chip.dataset.days);
    const error = form.querySelector("[data-error]");
    error.textContent = "";
    if (chip.hasAttribute("data-days-custom")) {  // "or after 3 days / 3 weeks / 2 months"
      const n = Number(form.elements.namedItem("n").value);
      days = n * Number(form.elements.namedItem("unit").value);
      if (!Number.isInteger(n) || n < 1 || days > 730) {
        error.textContent = t("fu_bad_days");
        form.elements.namedItem("n").focus();
        return;
      }
    }
    const item = { days };
    const other = form.elements.namedItem("other").value.trim();
    const what = form.querySelector('[name="what"]:checked').value;
    if (other) item.label = other;
    else if (what.startsWith("proc:")) item.procedure_id = Number(what.slice(5));
    else item.label = what.slice(6);
    addNext({ items: [item] });
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); if (adding.unknown) addNext(adding.unknown.body); });
  nextAdd.querySelector("[data-discard]").addEventListener("click", settle);
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

  // Suggestions as the doctor types (Saged, 2026-11-19): our own list, not the browser's <datalist>, whose
  // drop-down froze on Windows. From one letter; name or active ingredient; spelling variants match; a word that
  // starts with what was typed comes first; at most 8. A tap adds the medicine.
  const medInput = form.elements.namedItem("medicine");
  const plainText = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
    .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");
  const lineOf = (m) => ({ medicine_id: m.id, name: m.name, strength: m.strength, dose: m.default_dose, note: m.default_note });
  function addTyped(m) {
    const typed = medInput.value.trim();
    if (!m && !typed) return;
    m = m || medicines.find((x) => plainText(x.name) === plainText(typed));
    addLine(m ? lineOf(m) : { name: typed });
    medInput.value = "";
    list.replaceChildren();
    medInput.focus();
  }
  medInput.addEventListener("input", () => {
    const q = plainText(medInput.value.trim());
    const rank = (m) => {
      const text = plainText(`${m.name} ${m.generic || ""}`);
      if (!q) return -1;
      if (!text.includes(q)) return soundsLike(medInput.value, m.name) ? 3 : soundsLike(medInput.value, m.generic) ? 4 : -1;
      return text.startsWith(q) ? 0 : text.split(/[\s(),/-]+/).some((w) => w.startsWith(q)) ? 1 : 2;
    };
    const found = medicines.map((m) => [rank(m), m]).filter(([r]) => r >= 0)
      .sort((a, b) => a[0] - b[0]).slice(0, 8).map(([, m]) => m);
    list.replaceChildren(...found.map((m) => {
      const b = el("button", "chip-btn");
      b.type = "button";
      const name = el("bdi", null, [m.name, m.strength].filter(Boolean).join(" "));
      name.dir = "auto";
      b.append(name);
      b.addEventListener("click", () => addTyped(m));
      return b;
    }));
  });
  rxDialog.querySelector("[data-rx-add]").addEventListener("click", () => addTyped(null));
  medInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addTyped(null); }
    if (event.key === "ArrowDown") { const first = list.querySelector("button"); if (first) { event.preventDefault(); first.focus(); } }
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
