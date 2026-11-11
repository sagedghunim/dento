// Native <dialog>s (design 7.1, 7.6) and the browser half of safe repeats for their forms (design 7.4).
import { discardPending, errorMessage, get, isOk, newKey, save } from "./api.js";
import { clockParts } from "./numbers.js";
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
    if (this.discard) this.discard.hidden = true;
  }

  say(text) { if (this.error) this.error.textContent = text; }

  lock(on) {
    if (this.body) this.body.inert = on;
    this.form.querySelectorAll("input, select, textarea").forEach((field) => { field.readOnly = on; });
  }

  buttons(disabled) {
    this.form.querySelectorAll("[data-submit], [data-run], [data-finish-owed]").forEach((b) => { b.disabled = disabled; });
  }

  // Send, or resend the unconfirmed request. Returns the result for a definite answer, or null.
  async run(method, url, body, label) {
    if (this.dialog.dataset.busy) return null;
    const request = this.unknown || { method, url, body };
    this.dialog.dataset.busy = "1";
    this.buttons(true);
    this.lock(true);
    this.say("");
    const slow = setTimeout(() => this.say(t("still_saving")), 3000);
    const result = await save(request.method, request.url, request.body, {
      key: this.key, label: label || this.label, onWaiting: () => this.say(t("checking")),
    });
    clearTimeout(slow);
    delete this.dialog.dataset.busy;
    this.buttons(false);
    if (!result.settled) {
      this.unknown = request;
      this.say(result.signedOut ? t("signed_out") : t("unknown"));
      if (this.discard) this.discard.hidden = false;
      return null;
    }
    this.unknown = null;
    this.key = newKey();
    this.lock(false);
    if (this.discard) this.discard.hidden = true;
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

export class When {
  constructor(root, { onChange } = {}) {
    this.root = root;
    this.onChange = onChange || (() => {});
    this.slots = root.querySelector("[data-slots]");
    this.status = root.querySelector("[data-time-status]");
    this.extra = root.querySelector("[data-extra]");
    this.otherWrap = root.querySelector("[data-other-day]");
    this.other = root.querySelector('[name="other_day"]');
    this.info = { queue: false, slots: [] };
    this.seq = 0;
    root.addEventListener("change", (event) => {
      const name = event.target.name;
      if (name === "day") this.otherWrap.hidden = event.target.value !== "other";
      if (name === "doctor" || name === "day" || name === "other_day") this.refresh();
      if (name === "extra") this.markTaken();
      if (["doctor", "day", "other_day", "slot", "extra"].includes(name)) this.onChange(name);
    });
  }

  doctor() { const c = this.root.querySelector('[name="doctor"]:checked'); return c ? Number(c.value) : null; }

  day() {
    const c = this.root.querySelector('[name="day"]:checked');
    if (!c) return null;
    return c.value === "other" ? (this.other.value || null) : c.value;
  }

  slot() { const c = this.root.querySelector('[name="slot"]:checked'); return c && c.value ? c.value : null; }
  isExtra() { const c = this.root.querySelector('[name="extra"]'); return Boolean(c && c.checked && this.slot()); }

  setDoctor(id) {
    const input = this.root.querySelector(`[name="doctor"][value="${Number(id)}"]`);
    if (input && !input.closest("[hidden]")) input.checked = true;
  }

  setDay(value) {
    const chip = this.root.querySelector(`[name="day"][value="${CSS.escape(value)}"]`);
    if (chip) { chip.checked = true; this.otherWrap.hidden = true; return; }
    const other = this.root.querySelector('[name="day"][value="other"]');
    other.checked = true;
    this.other.value = value;
    this.otherWrap.hidden = false;
  }

  // Doctors outside the chosen section are hidden (a doctor's sections are those of their sessions).
  filterDoctors(sectionId) {
    this.root.querySelectorAll("[data-doctors] .chip").forEach((chip) => {
      const sections = (chip.dataset.sections || "").split(",").filter(Boolean);
      chip.hidden = Boolean(sectionId) && sections.length > 0 && !sections.includes(String(sectionId));
      if (chip.hidden) chip.querySelector("input").checked = false;
    });
  }

  needsTime() { return this.info.slots.length > 0 && !this.info.queue; }

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
    const chips = [];
    if (this.info.queue) chips.push(this.chip("", null, t("next_turn")));
    for (const slot of this.info.slots) chips.push(this.chip(slot.starts_at, slot.taken));
    if (this.info.slots.length) this.slots.replaceChildren(...chips);
    const free = this.info.slots.filter((s) => !s.taken).length;
    this.status.textContent = this.info.slots.length
      ? t("free_times", { n: free })
      : t("queue_day");
    this.extra.hidden = !this.info.slots.some((s) => s.taken);
    if (this.info.queue) chips[0].querySelector("input").checked = true;
    this.markTaken();
    this.onChange("slots");
  }

  chip(value, taken, text) {
    const label = el("label", "chip");
    const input = el("input");
    input.type = "radio";
    input.name = "slot";
    input.value = value;
    if (taken) input.dataset.taken = "1";
    const span = el("span");
    if (text) span.textContent = text;
    else span.append(clockNode(value));
    if (taken) span.append(el("small", null, t("taken")));
    label.append(input, span);
    return label;
  }

  markTaken() {
    const allow = this.root.querySelector('[name="extra"]').checked;
    this.slots.querySelectorAll("input[data-taken]").forEach((input) => {
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
