// Native <dialog>s (design 7.1, 7.6) and the browser half of safe repeats for their forms (design 7.4).
import { discardPending, errorMessage, get, isOk, newKey, save } from "./api.js";
import { asciiDigits, clockParts, localDigits } from "./numbers.js";
import { t } from "./strings.js";

// --- Opening and closing: focus returns to the button that opened the dialog ---

export function openDialog(dialog, opener) {
  dialog._opener = opener || document.activeElement;
  if (!dialog.open) dialog.showModal();
  const first = dialog.querySelector("[data-autofocus]:not([hidden])") || dialog.querySelector(".dialog-body input:not([type=hidden]):not([hidden]), .dialog-body button");
  if (first && first.offsetParent !== null) first.focus();
}

export function closeDialog(dialog) {
  if (dialog.dataset.busy) return false;  // a save in flight finishes first
  // An unknown outcome stays on screen until "Try again" or "Discard" (design 7.4, M1 decision R7).
  if (dialog.dataset.unknown) return false;
  if (dialog.open) dialog.close();
  const opener = dialog._opener;
  if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
  return true;
}

export function wireClose(dialog, onClose) {
  dialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) { event.preventDefault(); if (closeDialog(dialog) && onClose) onClose(); }
  });
  dialog.addEventListener("cancel", (event) => {  // Escape
    event.preventDefault();
    if (closeDialog(dialog) && onClose) onClose();
  });
  // Browsers may close a modal on Escape whatever the page says (without a recent tap, the cancel event
  // can't be cancelled). A dialog with a save in flight or an unknown outcome comes straight back (R7).
  dialog.addEventListener("close", () => {
    if ((dialog.dataset.unknown || dialog.dataset.busy) && !dialog.open) dialog.showModal();
  });
}

// Same-origin server HTML into nodes. <template> keeps it inert until inserted; no scripts are in fragments.
export function parseFragment(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  return template.content;
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The wall-clock time as nodes: digits isolated left to right, then AM/PM in the reading order.
export function clockNode(iso) {
  const parts = clockParts(iso);
  const span = el("span", "nowrap");
  if (!parts) return span;
  span.append(el("bdi", "ltr num", parts.time), " ", parts.pm ? t("pm") : t("am"));
  return span;
}

// --- One form, one request key until a definite answer ---
//
// The key is made when the dialog opens and reused for every send until a 2xx or a 4xx that
// wrote nothing. After a timeout or a network error the outcome is unknown: the fields lock,
// and only "Try again" (same key, same body) or "Discard" are offered.

export class Submitter {
  constructor(dialog, { label } = {}) {
    this.dialog = dialog;
    this.form = dialog.querySelector("form");
    this.body = dialog.querySelector(".dialog-body");
    this.error = dialog.querySelector("[data-error]");
    this.discard = dialog.querySelector("[data-discard]");
    this.submit = dialog.querySelector("[data-submit]");
    this.savedLabel = null;
    this.label = label;
    this.reset();
    if (this.discard) this.discard.addEventListener("click", () => this.giveUp());
    this.form.addEventListener("input", () => { if (!this.unknown) this.say(""); });
  }

  reset() {
    this.key = newKey();
    this.unknown = null;  // {method, url, body} while the outcome is unknown
    this.lock(false);
    this.say("");
    this.showUnknown(false);
  }

  // While unknown: no close, no other action; only "Try again" (the same key and body) or "Discard".
  showUnknown(on) {
    if (on) this.dialog.dataset.unknown = "1"; else delete this.dialog.dataset.unknown;
    if (this.discard) this.discard.hidden = !on;
    this.dialog.querySelectorAll("[data-close]").forEach((b) => { b.hidden = on; });
    this.dialog.querySelectorAll("[data-run], [data-finish-owed], [data-collect-now]").forEach((b) => { if (on) b.disabled = true; });
    if (this.submit && on && this.savedLabel == null) { this.savedLabel = this.submit.textContent; this.submit.textContent = t("try_again"); }
    if (this.submit && !on && this.savedLabel != null) { this.submit.textContent = this.savedLabel; this.savedLabel = null; }
  }

  // open() calls this first: a dialog that holds an unknown request reopens as it was, never with a new key.
  holdsUnknown() { return Boolean(this.unknown); }

  say(text) { if (this.error) this.error.textContent = text; }

  lock(on) {
    if (this.body) this.body.inert = on;
    this.form.querySelectorAll("input, select, textarea").forEach((field) => { field.readOnly = on; });
  }

  buttons(disabled) {
    this.form.querySelectorAll("[data-submit], [data-run], [data-finish-owed], [data-collect-now]").forEach((b) => { b.disabled = disabled; });
  }

  // Send, or resend the unconfirmed request. Returns the result for a definite answer, or null.
  // `headers` (such as If-Match) stay with the request, so "Try again" resends them.
  async run(method, url, body, label, { headers } = {}) {
    if (this.dialog.dataset.busy) return null;
    const request = this.unknown || { method, url, body, headers };
    this.dialog.dataset.busy = "1";
    this.buttons(true);
    this.lock(true);
    this.say("");
    const slow = setTimeout(() => this.say(t("still_saving")), 3000);
    if (!this.unknown) this.lastLabel = label || this.label;
    const result = await save(request.method, request.url, request.body, {
      key: this.key, label: this.lastLabel, headers: request.headers, onWaiting: () => this.say(t("checking")),
    });
    clearTimeout(slow);
    delete this.dialog.dataset.busy;
    window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
    this.buttons(false);
    if (!result.settled) {
      this.unknown = request;
      this.say(result.signedOut ? t("signed_out") : t("unknown"));
      this.showUnknown(true);
      return null;
    }
    this.unknown = null;
    this.key = newKey();
    this.lock(false);
    this.showUnknown(false);
    if (!isOk(result)) {
      this.say(errorMessage(result, t("failed")));
      return result;
    }
    return result;
  }

  giveUp() {
    discardPending(this.key);
    this.reset();
    this.say(t("discarded"));
  }
}

// --- Doctor, day and time: shared by booking and moving a booking ---
//
// The day is a month calendar (Saged, 2026-11-13): it opens on today's month and moves freely to any month
// up to the last bookable day. With a doctor picked, GET doctors/{id}/month greys their days off and hints
// how many times are free; the server decides "today" and which times have passed (design 5.11).

const pad = (n) => String(n).padStart(2, "0");
const isoOf = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;

export class When {
  constructor(root, { onChange } = {}) {
    this.root = root;
    this.onChange = onChange || (() => {});
    this.slots = root.querySelector("[data-slots]");
    this.status = root.querySelector("[data-time-status]");
    this.extra = root.querySelector("[data-extra]");
    this.cal = root.querySelector("[data-cal]");
    this.grid = root.querySelector("[data-cal-grid]");
    this.title = root.querySelector("[data-cal-title]");
    this.calStatus = root.querySelector("[data-cal-status]");
    this.dayInput = root.querySelector('[name="day"]');
    const names = (key) => (root.querySelector(`[data-cal-${key}]`).textContent || "").split("|");
    this.months = names("months");
    this.weekdays = names("weekdays");
    this.dayNames = names("days");
    this.today = this.cal.dataset.today;
    this.last = this.cal.dataset.last;
    this.monthInfo = new Map();  // "doctor|section|YYYY-MM" -> {day: {works, free, full}}
    this.info = { queue: false, slots: [] };
    this.seq = 0;
    this.monthSeq = 0;
    this.goTo(this.today);
    root.addEventListener("change", (event) => {
      const name = event.target.name;
      if (name === "doctor") { this.loadMonth(true); this.refresh(); }
      if (name === "extra") this.markTaken();
      if (["doctor", "slot", "extra"].includes(name)) this.onChange(name);
    });
    this.cal.addEventListener("click", (event) => {
      const move = event.target.closest("[data-cal-move]");
      const day = event.target.closest("[data-day]");
      if (move && !move.disabled) this.shift(Number(move.dataset.calMove));
      if (event.target.closest("[data-cal-today]")) this.pick(this.today);
      if (day && !day.disabled) this.pick(day.dataset.day);
    });
    this.grid.addEventListener("keydown", (event) => this.keys(event));
  }

  doctor() { const c = this.root.querySelector('[name="doctor"]:checked'); return c ? Number(c.value) : null; }
  day() { return this.dayInput.value || null; }
  slot() { const c = this.root.querySelector('[name="slot"]:checked'); return c && c.value ? c.value : null; }
  picked() { return Boolean(this.root.querySelector('[name="slot"]:checked')); }  // a time, or "next turn"
  isExtra() { const c = this.root.querySelector('[name="extra"]'); return Boolean(c && c.checked && this.slot()); }

  setDoctor(id) {
    const input = this.root.querySelector(`[name="doctor"][value="${Number(id)}"]`);
    if (input && !input.closest("[hidden]")) input.checked = true;
  }

  // Back to today, for a dialog opening afresh; what a month showed may have changed since.
  reset() {
    this.monthInfo.clear();
    this.dayInput.value = this.today;
    this.goTo(this.today);
  }

  setDay(value) {
    this.dayInput.value = value;
    this.goTo(value);
  }

  // "Thursday 19 November" in the page's language and the clinic's digits; the year only when it isn't this one.
  dayLabel(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    const weekday = this.dayNames[(new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 1) % 7];
    const year = iso.slice(0, 4) === this.today.slice(0, 4) ? "" : ` ${localDigits(y)}`;
    return `${weekday} ${localDigits(d)} ${this.months[m - 1]}${year}`;
  }

  // --- the calendar ---

  goTo(iso) {
    const [y, m] = iso.split("-").map(Number);
    this.view = { y, m };
    this.render();
    this.loadMonth(false);
  }

  shift(by) {
    let { y, m } = this.view;
    m += by;
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    const key = isoOf(y, m, 1).slice(0, 7);
    if (key < this.today.slice(0, 7) || key > this.last.slice(0, 7)) return;
    this.view = { y, m };
    this.render();
    this.loadMonth(false);
  }

  monthKey() { return isoOf(this.view.y, this.view.m, 1).slice(0, 7); }
  cacheKey(month) { return `${this.doctor() || ""}|${this.sectionOf ? this.sectionOf() : ""}|${month}`; }

  async loadMonth(doctorChanged) {
    const doctor = this.doctor();
    const month = this.monthKey();
    const key = this.cacheKey(month);
    const seq = ++this.monthSeq;
    if (!doctor || this.monthInfo.has(key)) { this.render(); if (doctorChanged) this.keepWorkingDay(); return; }
    this.calStatus.textContent = t("loading");
    const section = this.sectionOf ? this.sectionOf() : "";
    const result = await get(`/api/v1/doctors/${doctor}/month?month=${month}${section ? `&section=${section}` : ""}`);
    if (seq !== this.monthSeq) return;  // an older answer after a newer choice
    if (!isOk(result)) { this.render(); this.calStatus.textContent = errorMessage(result, t("failed")); return; }
    const days = {};
    for (const d of result.data.days) days[d.day] = d;
    this.monthInfo.set(key, days);
    this.render();
    if (doctorChanged) this.keepWorkingDay();
  }

  // One month's days for the picked doctor ({iso: {day, works, free, full}}), from the cache or the server; null
  // without a doctor or on a failed answer.
  async monthDays(month) {
    const doctor = this.doctor();
    if (!doctor) return null;
    const key = this.cacheKey(month);
    if (this.monthInfo.has(key)) return this.monthInfo.get(key);
    const section = this.sectionOf ? this.sectionOf() : "";
    const result = await get(`/api/v1/doctors/${doctor}/month?month=${month}${section ? `&section=${section}` : ""}`);
    if (!isOk(result) || doctor !== this.doctor()) return null;
    const days = {};
    for (const d of result.data.days) days[d.day] = d;
    this.monthInfo.set(key, days);
    return days;
  }

  // The first day from `from` on that the picked doctor works and still has room (Saged, 2026-11-24): a plan's
  // session opens there, not on a full day or a day off. Three months ahead at most; null when none is found.
  async firstOpen(from) {
    let [y, m] = from.split("-").map(Number);
    for (let i = 0; i < 3; i++) {
      const month = isoOf(y, m, 1).slice(0, 7);
      if (month > this.last.slice(0, 7)) break;
      const days = await this.monthDays(month);
      if (!days) return null;
      const found = Object.values(days).find((d) => d.day >= from && d.works && !d.full);
      if (found) return found.day;
      m += 1;
      if (m > 12) { m = 1; y += 1; }
    }
    return null;
  }

  // After a doctor change, a picked day they don't work moves to their next working day in the month shown.
  keepWorkingDay() {
    const days = this.monthInfo.get(this.cacheKey(this.monthKey()));
    const picked = this.day();
    if (!days || !picked || !days[picked] || days[picked].works) return;
    const next = Object.values(days).find((d) => d.works && d.day > picked);
    if (next) this.pick(next.day);
  }

  render() {
    const { y, m } = this.view;
    const month = this.monthKey();
    this.title.textContent = `${this.months[m - 1]} ${localDigits(y)}`;
    this.cal.querySelector('[data-cal-move="-1"]').disabled = month <= this.today.slice(0, 7);
    this.cal.querySelector('[data-cal-move="1"]').disabled = month >= this.last.slice(0, 7);
    const days = this.doctor() ? this.monthInfo.get(this.cacheKey(month)) : null;
    const cells = this.weekdays.map((name) => { const h = el("span", "cal-wd", name); h.setAttribute("aria-hidden", "true"); return h; });
    const lead = (new Date(Date.UTC(y, m - 1, 1)).getUTCDay() + 1) % 7;  // weeks start on Saturday
    for (let i = 0; i < lead; i++) cells.push(el("span", "cal-blank"));
    const count = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const picked = this.day();
    for (let d = 1; d <= count; d++) {
      const iso = isoOf(y, m, d);
      const info = days ? days[iso] : null;
      const button = el("button", "cal-day");
      button.type = "button";
      button.dataset.day = iso;
      const off = Boolean(info && !info.works && iso >= this.today);
      button.disabled = iso < this.today || iso > this.last || off;
      button.append(el("span", "cal-num", localDigits(d)));
      let hint = "";
      if (info && info.works) hint = info.full ? t("cal_full") : info.free ? t("cal_free", { n: localDigits(info.free) }) : "";
      button.append(el("small", "cal-hint", hint));
      if (info && info.full) button.classList.add("full");
      if (off) button.classList.add("off");
      if (iso === this.today) { button.classList.add("today"); button.setAttribute("aria-current", "date"); }
      // A plan's sessions (book.js): each one's suggested day is marked, the one being booked more strongly.
      const mark = this.marks ? this.marks.get(iso) : null;
      if (mark) button.classList.add("suggested", mark);
      button.setAttribute("aria-pressed", String(iso === picked));
      button.tabIndex = iso === picked ? 0 : -1;
      button.setAttribute("aria-label", [this.dayLabel(iso), mark ? t("cal_suggested") : "", off ? t("cal_off") : hint].filter(Boolean).join(" · "));
      cells.push(button);
    }
    this.grid.replaceChildren(...cells);
    if (!this.grid.querySelector('[tabindex="0"]')) { const first = this.grid.querySelector(".cal-day:not(:disabled)"); if (first) first.tabIndex = 0; }
    this.calStatus.textContent = picked ? t("picked_day", { day: this.dayLabel(picked) }) : t("need_day");
  }

  pick(iso) {
    if (iso < this.today || iso > this.last) return;
    const focused = this.cal.contains(document.activeElement) && document.activeElement.classList.contains("cal-day");
    this.dayInput.value = iso;
    if (iso.slice(0, 7) !== this.monthKey()) this.goTo(iso); else this.render();
    const button = this.grid.querySelector(`[data-day="${iso}"]`);
    if (button && focused) button.focus();
    this.refresh();
    this.onChange("day");
  }

  // Arrows move by a day or a week (right to left aware); Enter or Space picks, as on any button.
  keys(event) {
    const button = event.target.closest(".cal-day");
    if (!button) return;
    const rtl = getComputedStyle(this.grid).direction === "rtl";
    const steps = { ArrowLeft: rtl ? 1 : -1, ArrowRight: rtl ? -1 : 1, ArrowUp: -7, ArrowDown: 7 };
    if (!(event.key in steps)) return;
    event.preventDefault();
    const [y, m, d] = button.dataset.day.split("-").map(Number);
    const next = new Date(Date.UTC(y, m - 1, d + steps[event.key]));
    const iso = isoOf(next.getUTCFullYear(), next.getUTCMonth() + 1, next.getUTCDate());
    if (iso < this.today || iso > this.last) return;
    if (iso.slice(0, 7) !== this.monthKey()) this.goTo(iso);
    const target = this.grid.querySelector(`[data-day="${iso}"]`);
    if (target) { this.grid.querySelectorAll(".cal-day").forEach((b) => { b.tabIndex = -1; }); target.tabIndex = 0; target.focus(); }
  }

  // Doctors outside the chosen section are hidden (a doctor's sections are those of their sessions).
  filterDoctors(sectionId) {
    this.root.querySelectorAll("[data-doctors] .chip").forEach((chip) => {
      const sections = (chip.dataset.sections || "").split(",").filter(Boolean);
      chip.hidden = Boolean(sectionId) && sections.length > 0 && !sections.includes(String(sectionId));
      if (chip.hidden) chip.querySelector("input").checked = false;
    });
  }

  // A time, or "next turn" on a queue day, must be picked whenever the day offers any.
  needsTime() { return this.info.slots.length > 0 || this.info.queue; }

  async refresh() {
    const doctor = this.doctor();
    const day = this.day();
    const seq = ++this.seq;
    this.slots.replaceChildren();
    this.extra.hidden = true;
    this.info = { queue: false, slots: [] };
    if (!doctor || !day) { this.status.textContent = t("pick_doctor_day"); return; }
    this.status.textContent = t("loading_times");
    const section = this.sectionOf ? this.sectionOf() : "";  // in a center, the chosen section's sessions only
    const result = await get(`/api/v1/doctors/${doctor}/slots?day=${encodeURIComponent(day)}${section ? `&section=${section}` : ""}`);
    if (seq !== this.seq) return;  // an older answer after a newer choice
    if (!isOk(result)) { this.status.textContent = errorMessage(result, t("failed")); return; }
    this.info = { queue: result.data.queue, slots: result.data.slots || [] };
    if (!this.info.queue && !this.info.slots.length) { this.status.textContent = t("doctor_off"); this.onChange("slots"); return; }
    const chips = this.info.slots.map((slot) => this.chip(slot.starts_at, slot.taken, slot.passed));
    if (this.info.queue) chips.push(this.chip("", false, false, t("next_turn")));
    this.slots.replaceChildren(...chips);
    const free = this.info.slots.filter((s) => !s.taken && !s.passed).length;
    this.status.textContent = this.info.slots.length ? t("free_times", { n: localDigits(free) }) : t("queue_day");
    this.extra.hidden = !this.info.slots.some((s) => s.taken && !s.passed);
    this.markTaken();
    this.onChange("slots");
  }

  chip(value, taken, passed, text) {
    const label = el("label", "chip");
    const input = el("input");
    input.type = "radio";
    input.name = "slot";
    input.value = value;
    if (taken) input.dataset.taken = "1";
    if (passed) { input.dataset.passed = "1"; input.disabled = true; }
    const span = el("span");
    if (text) span.textContent = text;
    else span.append(clockNode(value));
    if (passed) span.append(el("small", null, t("passed")));
    else if (taken) span.append(el("small", null, t("taken")));
    label.append(input, span);
    return label;
  }

  // A taken time is offered only as an extra booking, on purpose; a time that has passed never is.
  markTaken() {
    const allow = this.root.querySelector('[name="extra"]').checked;
    this.slots.querySelectorAll("input[data-taken]:not([data-passed])").forEach((input) => {
      input.disabled = !allow;
      if (!allow && input.checked) input.checked = false;
    });
  }
}

// --- Printing: the tab is opened in the tap handler, where Safari allows it (design 7.5) ---

export function openPrintTab() {
  try { return window.open("", "_blank"); } catch (e) { return null; }
}

export function receiptUrl(invoiceId, paper) {
  let format = paper;
  if (!format) { try { format = localStorage.getItem("hamada.paper") || "a5"; } catch (e) { format = "a5"; } }
  return `/print/receipts/${invoiceId}?format=${format === "80mm" ? "80mm" : "a5"}&print=1`;
}

export function rememberPaper(paper) { try { localStorage.setItem("hamada.paper", paper); } catch (e) { /* none */ } }

// "This visit": a new treatment, or the next visit of one already started (Saged, 2026-11-22). Fills the select with
// the patient's treatments; `preferred` (a visit id) is chosen when it belongs to one of them.
// The treatment a new visit goes on with (Saged, 2026-11-22). The latest one seen in the last 60 days is proposed;
// the visit then takes that treatment's service (its next number shows it goes on), "A new treatment" starts at 1.
const PROPOSE_DAYS = 60;

export async function loadCourses(dialog, patientId, preferred = null, { propose = true } = {}) {
  const box = dialog.querySelector("[data-course-box]");
  if (!box) return;
  const select = box.querySelector("select");
  select.replaceChildren(select.options[0]);
  select.value = "";
  box.hidden = true;
  if (!patientId) return;
  const result = await get(`/api/v1/patients/${patientId}/courses`);
  if (!isOk(result) || !result.data.items.length) return;
  for (const c of result.data.items) {
    const option = document.createElement("option");
    option.value = String(c.id);
    option.dataset.service = String(c.service_id || "");
    option.textContent = t("course_option", { service: c.service, started: c.started, n: localDigits(c.last_no + 1) });
    select.append(option);
  }
  if (preferred && [...select.options].some((o) => o.value === String(preferred))) select.value = String(preferred);
  else if (!preferred && propose) {
    const latest = result.data.items[0];
    const since = (Date.parse(`${result.data.today}T00:00:00Z`) - Date.parse(`${latest.last_day}T00:00:00Z`)) / 86400000;
    if (since >= 0 && since <= PROPOSE_DAYS) select.value = String(latest.id);
  }
  box.hidden = false;
}

// The service of the chosen treatment, or null for "A new treatment".
export function courseService(dialog) {
  const select = dialog.querySelector('[name="continues_id"]');
  const option = select && select.value ? select.selectedOptions[0] : null;
  return option && option.dataset.service ? Number(option.dataset.service) : null;
}

// How many of the picked service (Saged, 2026-11-22), by its price-list unit: teeth, quadrants, jaws or units, each
// with its ceiling (the server checks the same, clinics/booking_services.COUNTED). A consultation, a whole case or a
// session is one, and the box is hidden. A procedure service outside the price list (no data-unit) counts units.
const COUNT_MAX = { tooth: 20, quadrant: 4, arch: 2, unit: 20 };

function unitOf(option) {
  if (!option || option.dataset.kind !== "procedure") return "";
  const unit = option.dataset.unit ?? "unit";
  return unit in COUNT_MAX ? unit : "";
}

export function showCount(box, option) {
  if (!box) return;
  const unit = unitOf(option);
  if (box.dataset.shown !== unit) box.querySelector("input").value = "1";  // another kind of count starts at 1
  box.dataset.shown = unit;
  box.hidden = !unit;
  if (!unit) return;
  box.querySelector("[data-qty-label]").textContent = box.dataset[unit];
  box.querySelector("input").max = String(COUNT_MAX[unit]);
}

export function countOf(box) {
  if (!box || box.hidden || !box.dataset.shown) return 1;
  const n = Number(asciiDigits(box.querySelector("input").value.trim()));
  return Number.isInteger(n) && n >= 1 && n <= COUNT_MAX[box.dataset.shown] ? n : 1;
}

// The price list's main services (Saged, 2026-11-22): the desk picks one (a chip), then one of its kinds in `select`.
// Options carry data-family; data-off marks those the section or doctor leaves out. Options without a family (the
// consultation) always show. Picking a main service with one kind picks it; otherwise the select's empty choice, or
// the first kind when it has none. `box` (optional) shows only while a main service is picked. A pick fires change.
export function wireFamilies(form, select, box = null) {
  const chips = form.querySelector("[data-families]");
  if (!select) return { refresh() {}, reset() {}, show: () => null };
  let current = "";
  const options = () => [...select.options].filter((o) => o.value);
  function apply() {
    for (const o of options()) {
      const visible = !o.dataset.off && (!o.dataset.family || o.dataset.family === current);
      o.hidden = o.disabled = !visible;  // Safari shows hidden options
    }
    for (const group of select.querySelectorAll("optgroup")) group.hidden = ![...group.children].some((o) => !o.hidden);
    if (chips) {
      for (const chip of chips.querySelectorAll("[data-family]")) {
        chip.hidden = !options().some((o) => o.dataset.family === chip.dataset.family && !o.dataset.off);
        chip.setAttribute("aria-pressed", String(chip.dataset.family === current));
      }
    }
    if (box) box.hidden = !current;
  }
  function pick(key) {
    current = key;
    apply();
    const kinds = options().filter((o) => o.dataset.family === key && !o.disabled);
    const empty = [...select.options].find((o) => !o.value);
    select.value = kinds.length === 1 || !empty ? (kinds[0] ? kinds[0].value : "") : "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    if (kinds.length > 1) select.focus();
  }
  if (chips) chips.addEventListener("click", (event) => { const chip = event.target.closest("[data-family]"); if (chip) pick(chip.dataset.family); });
  apply();
  return {
    refresh() { if (current && !options().some((o) => o.dataset.family === current && !o.dataset.off)) current = ""; apply(); },
    reset() { current = ""; apply(); },
    // Opens the main service of the option `id` (a treatment that goes on) and returns that option, or null.
    show(id) {
      const option = options().find((o) => o.value === String(id) && !o.dataset.off);
      if (!option) return null;
      current = option.dataset.family || current;
      apply();
      return option;
    },
  };
}

export function courseOf(dialog) {
  const select = dialog.querySelector('[name="continues_id"]');
  return select && select.value ? Number(select.value) : null;
}
