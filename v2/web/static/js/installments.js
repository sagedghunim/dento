// The patient file's installments (Saged, 2026-11-19). The dialog shows the parts as they will be made, then one
// POST patients/{id}/installments; "Cancel the installments" is one DELETE. The page is read again after each.
import { errorMessage, isOk, newKey, save } from "./api.js";
import { openDialog, wireClose } from "./dialogs.js";
import { amountInput, localDigits, moneyText, parsePiastres } from "./numbers.js";
import { toast } from "./shell.js";
import { t } from "./strings.js";

const card = document.querySelector("[data-installments]");
const dialog = document.getElementById("inst-dialog");
if (card && dialog) {
  const defaults = JSON.parse(document.getElementById("inst-defaults").textContent);
  const owed = Number(card.dataset.owed) || 0;
  const form = dialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const preview = dialog.querySelector("[data-inst-preview]");
  const error = dialog.querySelector("[data-error]");
  let key = null;
  wireClose(dialog);

  const every = () => (form.querySelector('[name="every"]:checked') || {}).value || "month";
  const addDays = (iso, days) => { const d = new Date(`${iso}T12:00:00`); d.setDate(d.getDate() + days); return d.toISOString().slice(0, 10); };
  const addMonths = (iso, n) => {
    const d = new Date(`${iso}T12:00:00`);
    const day = d.getDate();
    d.setDate(1); d.setMonth(d.getMonth() + n);
    d.setDate(Math.min(day, new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()));
    return d.toISOString().slice(0, 10);
  };
  const step = (iso, n) => (every() === "week" ? addDays(iso, 7 * n) : every() === "2weeks" ? addDays(iso, 14 * n) : addMonths(iso, n));

  // The same parts the server will make (billing/installments.schedule).
  function parts() {
    const down = parsePiastres(field("down").value) || 0;
    const amount = parsePiastres(field("amount").value) || 0;
    const first = field("first_due").value;
    if (down < 0 || down >= owed || amount <= 0 || !first) return null;
    const rest = owed - down;
    const count = Math.ceil(rest / amount);
    if (count > 60) return null;
    const out = down ? [[defaults.today, down]] : [];
    for (let n = 0; n < count; n++) out.push([step(first, n), Math.min(amount, rest - n * amount)]);
    return out;
  }

  function show() {
    const list = parts();
    preview.replaceChildren(...(list || []).map(([day, amount]) => {
      const li = document.createElement("li");
      li.textContent = `${new Date(`${day}T12:00:00`).toLocaleDateString(document.documentElement.lang, { day: "numeric", month: "long", year: "numeric" })} · ${moneyText(amount)}`;
      return li;
    }));
    error.textContent = list ? "" : t("inst_check");
  }

  card.addEventListener("click", async (event) => {
    if (event.target.closest("[data-inst-open]")) {
      form.reset();
      key = newKey();
      const down = Math.round((owed * defaults.down_percent) / 10000) * 100;
      const count = Math.max(1, defaults.count);
      field("down").value = down ? amountInput(down) : "";
      form.querySelector(`[name="every"][value="${defaults.every}"]`).checked = true;
      field("amount").value = amountInput(Math.ceil((owed - down) / count / 100) * 100);
      field("first_due").value = step(defaults.today, 1);
      dialog.querySelector("[data-inst-owed]").textContent = moneyText(owed);
      show();
      openDialog(dialog, event.target);
    }
    const cancel = event.target.closest("[data-inst-cancel]");
    if (cancel && window.confirm(t("inst_cancel_confirm"))) {
      cancel.disabled = true;
      const result = await save("DELETE", `/api/v1/patients/${card.dataset.patient}/installments`, null, { key: newKey(), label: t("inst_title") });
      if (result.settled && isOk(result)) { toast(t("inst_cancelled")); window.location.reload(); return; }
      cancel.disabled = false;
      card.querySelector("[data-inst-error]").textContent = errorMessage(result, t("failed"));
    }
  });
  form.addEventListener("input", show);
  form.addEventListener("change", (event) => {
    if (event.target.name === "every") field("first_due").value = step(defaults.today, 1);
    show();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!parts()) { show(); return; }
    const button = form.querySelector("[data-submit]");
    button.disabled = true;
    const body = { down: parsePiastres(field("down").value) || 0, every: every(), amount: parsePiastres(field("amount").value),
                   first_due: field("first_due").value };
    const result = await save("POST", `/api/v1/patients/${card.dataset.patient}/installments`, body, { key, label: t("inst_title") });
    button.disabled = false;
    if (result.settled && isOk(result)) {
      dialog.close();
      toast(t("inst_saved", { n: localDigits(result.data.plan.parts.length) }));
      window.location.reload();
      return;
    }
    error.textContent = errorMessage(result, t("failed"));
  });
}

// "Pay an installment" (Saged, 2026-11-22): any amount toward the plan, one POST. More than is due by today splits
// what is left again evenly over the coming parts (billing/installments.respread); the dialog says how first.
const payDialog = document.getElementById("inst-pay-dialog");
const plan = document.getElementById("inst-plan") ? JSON.parse(document.getElementById("inst-plan").textContent) : null;
if (card && payDialog && plan) {
  const form = payDialog.querySelector("form");
  const amountField = form.elements.namedItem("amount");
  const note = payDialog.querySelector("[data-inst-pay-note]");
  const error = payDialog.querySelector("[data-error]");
  const today = JSON.parse(document.getElementById("inst-defaults").textContent).today;
  const dueNow = plan.parts.filter((p) => p.due_on <= today).reduce((sum, p) => sum + p.amount - p.paid, 0);
  const next = plan.parts.find((p) => p.paid < p.amount);
  let key = null;
  wireClose(payDialog);

  // The same split as the server: each rounded up to a whole pound, the last smaller.
  function split(rest, count) {
    let each = Math.ceil(rest / count);
    each = Math.ceil(each / 100) * 100;
    const out = [];
    while (rest > 0 && out.length < count) { out.push(Math.min(each, rest)); rest -= out[out.length - 1]; }
    return out;
  }

  function explain() {
    const amount = parsePiastres(amountField.value) || 0;
    const rest = plan.left - amount;
    const coming = plan.parts.filter((p) => p.due_on > today && p.paid < p.amount).length;
    note.hidden = !(amount > dueNow && rest > 0 && coming);
    if (!note.hidden) note.textContent = t("inst_respread", { rest: moneyText(rest), parts: split(rest, coming).map(moneyText).join(" + ") });
    error.textContent = amount > plan.left ? t("inst_over") : "";
  }

  card.addEventListener("click", (event) => {
    if (!event.target.closest("[data-inst-pay]")) return;
    form.reset();
    key = newKey();
    const suggested = dueNow > 0 ? dueNow : next ? next.amount - next.paid : 0;
    amountField.value = suggested ? amountInput(suggested) : "";
    explain();
    openDialog(payDialog, event.target);
  });
  form.addEventListener("input", explain);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const amount = parsePiastres(amountField.value);
    if (!amount || amount <= 0 || amount > plan.left) { error.textContent = amount > plan.left ? t("inst_over") : t("bad_amount"); amountField.focus(); return; }
    const button = form.querySelector("[data-submit]");
    button.disabled = true;
    const method = (form.querySelector('[name="method"]:checked') || {}).value || "cash";
    const result = await save("POST", `/api/v1/patients/${card.dataset.patient}/installments/payments`, { amount, method },
      { key, label: t("inst_title") });
    button.disabled = false;
    if (result.settled && isOk(result)) {
      payDialog.close();
      toast(t("inst_paid", { amount: moneyText(amount) }));
      window.location.reload();
      return;
    }
    error.textContent = errorMessage(result, t("failed"));
  });
}
