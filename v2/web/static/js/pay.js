// The Pay dialog (design 7.3, 5.3): what is owed, the bill's items, payments, discount, refund and void,
// each offered by permission. Every change is one request with its own key (dialogs.Submitter).
import { errorMessage, get, isOk } from "./api.js";
import { Submitter, clockNode, el, openDialog, receiptUrl, rememberPaper, wireClose } from "./dialogs.js";
import { amountInput, localDigits, moneyNode, moneyText, parsePiastres, parseWhole } from "./numbers.js";
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
  wireClose(dialog);

  const money = (p) => moneyNode(p, t("money_pattern"));

  function sums() {
    const s = invoice || { total: visit.fee, discount: 0, paid: 0, due: visit.due };
    for (const key of ["total", "discount", "paid", "due"]) dialog.querySelector(`[data-sum="${key}"]`).replaceChildren(money(s[key]));
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
      lines.replaceChildren(...invoice.lines.map((line) => {
        const li = el("li");
        const name = line.description || (services[line.service_id] && services[line.service_id].name) || visit.service || "";
        const label = el("bdi", null, line.qty > 1 ? `${name} × ${localDigits(line.qty)}` : name);
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
    if (field("discount")) {
      field("discount").closest(".subsection").hidden = !open;
      if (!field("discount").dataset.touched) field("discount").value = amountInput(invoice ? invoice.discount : 0);
    }
    // Payments and refunds
    const payments = invoice ? invoice.payments : [];
    dialog.querySelector("[data-payments-section]").hidden = !payments.length;
    dialog.querySelector("[data-payments]").replaceChildren(...payments.map((p) => {
      const li = el("li");
      const what = el("span");
      what.append(p.kind === "refund" ? t("refund_row") : methodName(p.method), " · ", clockNode(p.received_at));
      if (p.refunded) what.append(" · ", t("refunded_part"), " ", money(p.refunded));
      const amt = el("span", "amt");
      amt.append(p.kind === "refund" ? "−" : "", money(p.amount));
      li.append(what, amt);
      return li;
    }));
    const refundable = payments.filter((p) => p.kind === "payment" && p.amount > p.refunded);
    const refund = dialog.querySelector("[data-refund]");
    if (refund) {
      refund.hidden = !refundable.length;
      field("refund_of").replaceChildren(...refundable.map((p) => {
        const option = el("option", null, `${methodName(p.method)} · ${moneyText(p.amount - p.refunded)}`);
        option.value = p.id;
        option.dataset.left = p.amount - p.refunded;
        return option;
      }));
      if (refundable.length && !field("refund_amount").dataset.touched) field("refund_amount").value = amountInput(refundable[0].amount - refundable[0].refunded);
    }
    const voidBox = dialog.querySelector("[data-void]");
    if (voidBox) voidBox.hidden = !(invoice && open && invoice.paid === 0);
    dialog.querySelectorAll("[data-print]").forEach((b) => { b.hidden = !invoice || invoice.status !== "open"; });
    status.textContent = invoice && invoice.status === "void" ? t("bill_void") : invoice ? t("bill_no", { n: localDigits(invoice.number) }) : "";
  }

  function methodName(code) {
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
      if (result && isOk(result)) { invoice = result.data.invoice; delete field("discount").dataset.touched; delete field("amount").dataset.touched; render(); toast(t("discount_saved")); }
      if (result && result.status === 409) await load();  // someone changed it meanwhile: show the current value
    } else if (name === "refund") {
      const option = field("refund_of").selectedOptions[0];
      const amount = parsePiastres(field("refund_amount").value);
      if (!option || !amount) { submitter.say(t("bad_amount")); return; }
      result = await submitter.run("POST", `/api/v1/payments/${option.value}/refund`, { amount }, [t("refund_row"), visit.name, moneyText(amount)].join(" · "));
      if (result && isOk(result)) { toast(t("refunded", { amount: moneyText(amount) })); delete field("refund_amount").dataset.touched; await load(); }
    } else if (name === "void") {
      result = await submitter.run("POST", `/api/v1/invoices/${invoice.id}/void`, {}, t("bill"));
      if (result && isOk(result)) { invoice = result.data.invoice; render(); toast(t("bill_voided")); }
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
    form.reset();
    submitter.reset();
    unknownState(false);
    form.querySelectorAll("[data-touched]").forEach((f) => { delete f.dataset.touched; });
    const name = dialog.querySelector("[data-name]");
    name.replaceChildren(el("bdi", null, visit.name));
    name.firstChild.dir = "auto";
    dialog.querySelector("[data-meta]").textContent = visit.service || "";
    render();
    openDialog(dialog);
    load();
  }

  return { open };
}
