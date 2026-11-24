// Old debt and credit when a booking or a walk-in collects (Saged, 2026-11-24). The quote says what the patient owes
// from before and holds as credit; the box says it in one line ("owes 240 from before · all of it 540", or "has 60
// credit · collect 240") and proposes that amount. More than that asks once, in amber: keep the rest as the
// patient's credit, or take just what is owed. The server checks the same (billing.plan_booking_money).
import { amountInput, moneyText, parsePiastres } from "./numbers.js";
import { t } from "./strings.js";

export function moneyBox(dialog, form) {
  const line = dialog.querySelector("[data-money-line]");
  const over = dialog.querySelector("[data-over]");
  const amount = form.elements.namedItem("amount");
  let fee = null;
  let owed = 0;
  let credit = 0;
  let most = null;  // what pays everything; the server's number when it refused one
  let keep = false;  // the desk chose to keep what is over as credit

  const typed = () => parsePiastres(amount.value) || 0;

  function show() {
    if (!line) return;
    const parts = [];
    if (owed > 0) parts.push(t("money_owed", { amount: moneyText(owed) }));
    if (credit > 0) parts.push(t("money_credit", { amount: moneyText(credit) }));
    if (parts.length && most != null) parts.push(t(credit > 0 ? "money_ask" : "money_all", { amount: moneyText(most) }));
    line.textContent = parts.join(" · ");
    line.hidden = !parts.length;
    const extra = most == null ? 0 : typed() - most;
    over.hidden = extra <= 0;
    if (extra <= 0) { keep = false; return; }
    over.querySelector("[data-over-text]").textContent = keep
      ? t("over_kept", { amount: moneyText(extra) })
      : t("over_text", { most: moneyText(most), typed: moneyText(typed()) });
    const keepButton = over.querySelector("[data-keep-credit]");
    keepButton.textContent = t("over_keep", { typed: moneyText(typed()), extra: moneyText(extra) });
    keepButton.hidden = keep;
    over.querySelector("[data-keep-most]").textContent = t("over_most", { most: moneyText(most) });
  }

  if (amount) amount.addEventListener("input", () => { keep = false; show(); });
  if (over) {
    over.querySelector("[data-keep-credit]").addEventListener("click", () => { keep = true; show(); });
    over.querySelector("[data-keep-most]").addEventListener("click", () => {
      amount.value = amountInput(most);
      amount.dataset.touched = "1";
      keep = false;
      show();
      amount.focus();
    });
  }

  return {
    // The quote's answer and the visit's fee (null when unknown or hidden). Returns the amount to propose.
    set(data, visitFee) {
      fee = visitFee;
      owed = (data && data.owed) || 0;
      credit = (data && data.credit) || 0;
      most = fee == null ? null : Math.max(0, fee + owed - credit);
      keep = false;
      show();
      return most;
    },
    reset() { fee = null; owed = 0; credit = 0; most = null; keep = false; show(); },
    // Something to collect even with no fee: an old debt.
    owes: () => owed > 0,
    // Adds keep_credit to the payment when the desk chose it.
    fill(payment) { if (keep && most != null && payment.amount > most) payment.keep_credit = true; return payment; },
    // What stops the payment: more than everything with no choice made, or nothing with no credit to pay it.
    problem(payment) {
      if (most != null && payment.amount > most && !keep) return t("over_pick");
      if (!payment.amount && !(credit > 0)) return t("bad_amount");
      return "";
    },
    // The server refused more than is owed (the numbers changed meanwhile): its own most, asked again.
    refused(result) {
      const error = result && result.data && result.data.error;
      if (!error || error.code !== "over_balance" || !Number.isInteger(error.most)) return false;
      most = error.most;
      keep = false;
      show();
      return true;
    },
  };
}
