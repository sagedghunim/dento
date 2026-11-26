// The Pay dialog (design 7.3, 5.3): what is owed, the bill's items, payments, discount and refund,
// each offered by permission. Every change is one request with its own key (dialogs.Submitter).
// Discount types (Saged, 2026-11-13): the clinic's types are chips, read for this visit each time the dialog opens
// with the one the doctor suggested. One tap applies a type, a tap on the applied one removes it; the request
// carries the discount the screen shows, so two people at once take effect once (409 `stale` reloads the bill).
import { errorMessage, get, isOk } from "./api.js";
import { Submitter, clockNode, el, openDialog, receiptUrl, rememberPaper, wireClose } from "./dialogs.js";
import { amountInput, localDigits, moneyNode, moneyText, parsePiastres, parseWhole, percentText } from "./numbers.js";
import { t } from "./strings.js";
import { toast } from "./shell.js";

const config = JSON.parse(document.getElementById("clinic-config").textContent);
const perms = config.perms;
const services = Object.fromEntries(config.services.map((s) => [s.id, s]));

export function initPay({ onChanged } = {}) {
  const dialog = document.getElementById("pay-dialog");
  if (!dialog) return null;
  const form = dialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const submitter = new Submitter(dialog, { label: t("payment") });
  const status = dialog.querySelector("[data-status]");
  const retry = dialog.querySelector("[data-retry]");
  let visit = null;    // {id, name, service, due, fee, status}
  let invoice = null;  // the full invoice once it exists
  let types = [];      // the clinic's active discount types: {id, name, kind, value, allowed}
  let suggested = null;  // {id, name}: the type the doctor suggested for this visit
  const discountSection = dialog.querySelector("[data-discount-section]");
  const typeChips = dialog.querySelector("[data-discount-types]");
  const suggestedNote = dialog.querySelector("[data-suggested]");
  wireClose(dialog);

  const money = (p) => moneyNode(p, t("money_pattern"));
  const creditLabel = dialog.querySelector("[data-credit-row] dt").textContent;
  // A payment's words: a booking deposit says so, with its day when that isn't today (Saged, 2026-11-16).
  const dayShort = (iso) => localDigits(`${Number(iso.slice(8, 10))}/${Number(iso.slice(5, 7))}`);
  const paymentName = (p) => (p.deposit ? `${t("deposit")} · ${methodName(p.method)}` : methodName(p.method));

  function sums() {
    const s = invoice || { total: visit.fee, discount: 0, paid: 0, due: visit.due, credit: 0 };
    for (const key of ["total", "discount", "paid", "due", "credit"]) dialog.querySelector(`[data-sum="${key}"]`).replaceChildren(money(s[key] || 0));
    // More paid than the bill (a deposit over a smaller bill): the desk refunds it by hand.
    // A no-show's kept deposit is named a deposit, not an overpayment (Saged, 2026-11-17).
    const creditRow = dialog.querySelector("[data-credit-row]");
    creditRow.hidden = !(s.credit > 0);
    creditRow.querySelector("dt").textContent = visit.status === "no_show" ? t("deposit") : creditLabel;
    // Give back what was paid over the bill beyond a deposit still held (the server works out the same).
    const giveBack = dialog.querySelector("[data-give-back-row]");
    if (giveBack) giveBack.hidden = !(invoice && invoice.status === "open" && invoice.paid - Math.max(invoice.total, invoice.deposit || 0) > 0);
    return s;
  }

  function render() {
    const s = sums();
    const open = !invoice || invoice.status === "open";
    dialog.querySelector("[data-collect]").hidden = !(perms.collect && open && s.due > 0 && visit.status !== "cancelled");
    if (!field("amount").dataset.touched) field("amount").value = amountInput(s.due);
    // Items
    const lines = dialog.querySelector("[data-lines]");
    if (invoice) {
      // A line at 0 next to charged ones (a free consultation with a paid procedure) goes (Saged, 2026-11-22).
      const charged = invoice.lines.filter((line) => line.amount);
      lines.replaceChildren(...(charged.length ? charged : invoice.lines).map((line) => {
        const li = el("li");
        const name = line.description || (services[line.service_id] && services[line.service_id].name) || visit.service || "";
        const label = el("bdi", null, line.qty > 1 ? (line.unit_price != null ? `${name} · ${localDigits(line.qty)} × ${moneyText(line.unit_price)}` : `${name} × ${localDigits(line.qty)}`) : name);
        label.dir = "auto";
        const right = el("span", "amt");
        right.append(money(line.amount));
        li.append(label, right);
        // Only lines this person may remove, as the server says per line (M1 decision R3).
        if (open && line.removable === true) {
          const remove = el("button", "btn danger", t("remove"));
          remove.type = "button";
          remove.dataset.run = "remove-line";
          remove.dataset.line = line.id;
          remove.setAttribute("aria-label", `${t("remove")}: ${name}`);
          li.append(remove);
        }
        return li;
      }));
    } else {
      const li = el("li");
      const label = el("bdi", null, visit.service || "");
      label.dir = "auto";
      const right = el("span", "amt");
      right.append(money(visit.fee));
      li.append(label, right);
      lines.replaceChildren(li);
    }
    form.querySelectorAll('[data-run="add-line"]').forEach((b) => { b.closest(".form-row").hidden = !(perms.collect && open); });
    renderTypes(open);
    if (field("discount") && !field("discount").dataset.touched) field("discount").value = amountInput(invoice ? invoice.discount : 0);
    // Payments and refunds
    const payments = invoice ? invoice.payments : [];
    dialog.querySelector("[data-payments-section]").hidden = !payments.length;
    dialog.querySelector("[data-payments]").replaceChildren(...payments.map((p) => {
      const li = el("li");
      const what = el("span");
      what.append(p.kind === "refund" ? t("given_back_row") : paymentName(p), " · ");  // done, not an order
      if (p.day && p.day !== config.today) what.append(dayShort(p.day), " ");
      what.append(clockNode(p.received_at));
      if (p.refunded) what.append(" · ", t("refunded_part"), " ", money(p.refunded));
      const amt = el("span", "amt");
      amt.append(p.kind === "refund" ? "−" : "", money(p.amount));
      li.append(what, amt);
      return li;
    }));
    const refund = dialog.querySelector("[data-refund]");
    if (refund) {
      // A no-show gives back only the deposit (when the owner allows it); every other payment stays.
      const refundable = payments.filter((p) => p.kind === "payment" && p.amount > p.refunded && (visit.status !== "no_show" || p.deposit));
      // Only before going in (Saged, 2026-11-26); a no-show's deposit when the owner allows it. After going in,
      // only what was paid over the bill (a deposit over a smaller bill). The server checks the same.
      const before = ["booked", "waiting"].includes(visit.status) || (visit.status === "no_show" && config.deposit_back_no_show);
      const over = ["booked", "waiting", "with_assistant", "ready", "in_room", "done"].includes(visit.status) && invoice ? Math.max(0, invoice.paid - invoice.total) : 0;
      refund.hidden = !refundable.length || !(before || over > 0);
      // Before going in, all of the visit's money goes back at once and the visit is cancelled (Saged, 2026-11-26):
      // the amount is the whole bill's paid money, not typed. Paid over the bill: that part is offered first (it goes
      // back alone); before going in, all of it may still be typed.
      const whole = ["booked", "waiting"].includes(visit.status) && !over;
      const most = (p) => (whole ? invoice.paid : before && !over ? p.amount - p.refunded : Math.min(p.amount - p.refunded, over));
      field("refund_of").replaceChildren(...refundable.map((p) => {
        const option = el("option", null, `${paymentName(p)} · ${moneyText(p.amount - p.refunded)}`);
        option.value = p.id;
        option.dataset.left = most(p);
        return option;
      }));
      field("refund_of").closest("label").hidden = whole;  // one refund takes every payment
      field("refund_amount").readOnly = whole;
      dialog.querySelector("[data-refund-all]").hidden = !whole;
      if (whole) delete field("refund_amount").dataset.touched;
      if (refundable.length && !field("refund_amount").dataset.touched) field("refund_amount").value = amountInput(most(refundable[0]));
    }
    dialog.querySelectorAll("[data-print]").forEach((b) => { b.hidden = !invoice || invoice.status !== "open"; });
    status.textContent = invoice && invoice.status === "void" ? t("bill_void") : invoice ? t("bill_no", { n: localDigits(invoice.number) }) : "";
  }

  // The discount types as chips. The applied one is pressed (also when its type was stopped since, so it can
  // still be removed); the doctor's suggestion is marked and named. A chip this person may not use is disabled
  // and carries no data-run, so the Submitter never enables it; the server checks every tap again.
  function renderTypes(open) {
    const applied = invoice && invoice.discount_type;
    const shown = types.slice();
    if (applied && !shown.some((d) => d.id === applied.id)) {
      shown.push({ id: applied.id, name: applied.name, kind: applied.percent ? "percent" : "fixed",
                   value: applied.percent || invoice.discount, allowed: true });
    }
    const locked = Boolean(invoice && invoice.discount_changeable === false);
    typeChips.replaceChildren(...shown.map((d) => {
      const on = Boolean(applied && applied.id === d.id);
      const chip = el("button", "chip-btn");
      chip.type = "button";
      chip.dataset.type = d.id;
      chip.setAttribute("aria-pressed", String(on));
      const name = el("bdi", null, d.name);
      name.dir = "auto";
      chip.append(name, " ", el("span", "count num", d.kind === "percent" ? percentText(d.value) : moneyText(d.value)));
      if (suggested && suggested.id === d.id) chip.classList.add("suggested");
      if (d.allowed && !locked) chip.dataset.run = "discount-type"; else chip.disabled = true;
      return chip;
    }));
    suggestedNote.hidden = !suggested;
    suggestedNote.textContent = suggested ? t("doctor_suggested", { name: suggested.name }) : "";
    discountSection.hidden = !(open && visit.status !== "cancelled" && (shown.length || field("discount")));
  }

  async function loadTypes() {
    const forVisit = visit.id;
    const result = await get(`/api/v1/appointments/${forVisit}/discount-types`);
    if (!visit || visit.id !== forVisit || !isOk(result)) return;  // none offered; the typed amount still works
    types = result.data.items;
    suggested = result.data.suggested;
    render();
  }

  function methodName(code) {
    if (code === "credit") return t("from_credit");  // the patient's credit moved from another bill (Saged, 2026-11-24)
    const chip = form.querySelector(`[name="method"][value="${code}"]`);
    return chip ? chip.nextElementSibling.textContent : code;
  }

  async function load() {
    if (!visit.invoiceId) { invoice = null; render(); return; }
    status.textContent = t("loading");
    const result = await get(`/api/v1/invoices/${visit.invoiceId}`);
    if (!isOk(result)) { status.textContent = errorMessage(result, t("failed")); return; }
    invoice = result.data.invoice;
    render();
  }

  async function ensureInvoice() {
    if (invoice) return invoice;
    const result = await submitter.run("POST", `/api/v1/appointments/${visit.id}/invoice`, {}, t("bill"));
    if (!result || !isOk(result)) return null;
    invoice = result.data.invoice;
    visit.invoiceId = invoice.id;
    return invoice;
  }

  function unknownState(on) {
    retry.hidden = !on;
    form.querySelectorAll("[data-run]").forEach((b) => { b.disabled = on; });
  }

  async function act(name, button) {
    let result = null;
    if (name === "pay") {
      const amount = parsePiastres(field("amount").value);
      if (!amount) { submitter.say(t("bad_amount")); return; }
      const method = form.querySelector('[name="method"]:checked').value;
      result = await submitter.run("POST", `/api/v1/appointments/${visit.id}/payments`, { amount, method }, [t("payment"), visit.name, moneyText(amount)].join(" · "));
      if (result && isOk(result)) {
        toast(t("collected", { amount: moneyText(amount), name: visit.name }));
        visit.invoiceId = result.data.invoice.id;
        field("amount").dataset.touched = "";
        delete field("amount").dataset.touched;
        await load();
      }
    } else if (name === "add-line") {
      const serviceId = Number(field("line_service").value);
      const qty = parseWhole(field("qty").value);
      if (!qty) { submitter.say(t("bad_qty")); return; }
      if (!(await ensureInvoice())) { afterUnknown(); return; }
      const kind = services[serviceId] && services[serviceId].kind === "procedure" ? "procedure" : "service";
      result = await submitter.run("POST", `/api/v1/invoices/${invoice.id}/lines`, { kind, service_id: serviceId, qty }, `${t("bill")} · ${visit.name}`);
      if (result && isOk(result)) { invoice = result.data.invoice; delete field("amount").dataset.touched; render(); }
    } else if (name === "remove-line") {
      result = await submitter.run("DELETE", `/api/v1/invoices/${invoice.id}/lines/${button.dataset.line}`, {}, `${t("bill")} · ${visit.name}`);
      if (result && result.status === 202) toast(t("sent_for_permission"));  // stays until the owner approves
      if (result && isOk(result)) { invoice = result.data.invoice; delete field("amount").dataset.touched; render(); }
      if (result && result.status === 403) {  // the rule changed or the line isn't ours: show the bill as it is
        const message = errorMessage(result, t("not_removable"));
        await load();
        submitter.say(message);
      }
    } else if (name === "discount") {
      const discount = parsePiastres(field("discount").value);
      if (discount === null) { submitter.say(t("bad_amount")); return; }
      if (!(await ensureInvoice())) { afterUnknown(); return; }
      result = await submitter.run("PUT", `/api/v1/invoices/${invoice.id}/discount`, { discount, expected_discount: invoice.discount }, `${t("bill")} · ${visit.name}`);
      if (result && result.status === 202) { toast(t("sent_for_permission")); await load(); return; }
      if (result && isOk(result)) { invoice = result.data.invoice; delete field("discount").dataset.touched; delete field("amount").dataset.touched; render(); toast(t("discount_saved")); }
      if (result && result.status === 409) await load();  // someone changed it meanwhile: show the current value
    } else if (name === "discount-type") {
      const typeId = Number(button.dataset.type);
      const label = button.querySelector("bdi").textContent;
      if (!(await ensureInvoice())) { afterUnknown(); return; }
      const remove = Boolean(invoice.discount_type && invoice.discount_type.id === typeId);
      result = await submitter.run("PUT", `/api/v1/invoices/${invoice.id}/discount-type`,
        { type_id: remove ? null : typeId, expected_discount: invoice.discount }, [t("bill"), visit.name, label].join(" · "));
      if (result && isOk(result)) {
        invoice = result.data.invoice;
        if (field("discount")) delete field("discount").dataset.touched;
        delete field("amount").dataset.touched;
        render();
        toast(remove ? t("discount_removed") : t("discount_applied", { name: label }));
      }
      if (result && [403, 404, 409].includes(result.status)) {  // changed meanwhile, or no longer offered: show it as it is
        const message = errorMessage(result, t("failed"));
        await Promise.all([load(), loadTypes()]);
        submitter.say(message);
      }
    } else if (name === "refund") {
      const option = field("refund_of").selectedOptions[0];
      const amount = parsePiastres(field("refund_amount").value);
      if (!option || !amount) { submitter.say(t("bad_amount")); return; }
      result = await submitter.run("POST", `/api/v1/payments/${option.value}/refund`, { amount }, [t("refund_row"), visit.name, moneyText(amount)].join(" · "));
      if (result && result.status === 202) { toast(t("sent_for_permission")); await load(); return; }
      if (result && isOk(result)) { toast(t(result.data.invoice && result.data.invoice.status === "void" ? "refunded_cancelled" : "refunded", { amount: moneyText(amount) })); delete field("refund_amount").dataset.touched; await load(); }
    } else if (name === "give-back") {
      result = await submitter.run("POST", `/api/v1/invoices/${invoice.id}/give-back`, {}, [t("give_back"), visit.name].join(" · "));
      if (result && isOk(result)) { toast(t("given_back")); await load(); }
    }
    afterUnknown();
    if (result && isOk(result) && onChanged) onChanged();
  }

  function afterUnknown() { unknownState(Boolean(submitter.unknown)); }

  form.addEventListener("click", (event) => {
    const button = event.target.closest("[data-run]");
    if (button && !button.disabled) { event.preventDefault(); act(button.dataset.run, button); }
    const paper = event.target.closest("[data-print]");
    if (paper && invoice) {
      rememberPaper(paper.dataset.print);
      window.open(receiptUrl(invoice.id, paper.dataset.print), "_blank");
    }
  });
  retry.addEventListener("click", async () => {
    const result = await submitter.run();
    afterUnknown();
    if (result && isOk(result)) { await load(); if (onChanged) onChanged(); }
  });
  dialog.querySelector("[data-discard]").addEventListener("click", () => { afterUnknown(); load(); });
  form.addEventListener("input", (event) => { if (event.target.name) event.target.dataset.touched = "1"; });
  form.addEventListener("change", (event) => {
    if (event.target.name === "refund_of") {
      const option = event.target.selectedOptions[0];
      if (option) field("refund_amount").value = amountInput(Number(option.dataset.left));
    }
  });
  form.addEventListener("submit", (event) => event.preventDefault());

  function open(v) {
    if (submitter.holdsUnknown()) { openDialog(dialog); return; }  // never a new key over an unknown one (R7)
    visit = { ...v };
    invoice = null;
    types = [];
    suggested = null;
    form.reset();
    submitter.reset();
    unknownState(false);
    form.querySelectorAll("[data-touched]").forEach((f) => { delete f.dataset.touched; });
    const name = dialog.querySelector("[data-name]");
    name.replaceChildren(el("bdi", null, visit.name));
    name.firstChild.dir = "auto";
    dialog.querySelector("[data-meta]").textContent = visit.service || "";
    const earlier = dialog.querySelector("[data-earlier-note]");  // owed on older visits, shown, not mixed in
    earlier.hidden = !(visit.earlier > 0);
    earlier.textContent = visit.earlier > 0 ? t("owed_before", { amount: moneyText(visit.earlier) }) : "";
    render();
    openDialog(dialog);
    load();
    if (perms.collect) loadTypes();
  }

  return { open };
}
