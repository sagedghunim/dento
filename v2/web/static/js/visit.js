// Finish (with or without the payment), cancel and move a booking (design 5.2). Each dialog names the
// patient, and owns one request key until a definite answer.
import { isOk } from "./api.js";
import { Submitter, When, el, openDialog, wireClose } from "./dialogs.js";
import { amountInput, moneyText, parsePiastres } from "./numbers.js";
import { t } from "./strings.js";
import { toast } from "./shell.js";

function named(dialog, name) {
  const holder = dialog.querySelector("[data-name]");
  holder.replaceChildren(el("bdi", null, name));
  holder.firstChild.dir = "auto";
}

export function initVisitDialogs({ onChanged } = {}) {
  const finishDialog = document.getElementById("finish-dialog");
  const cancelDialog = document.getElementById("cancel-dialog");
  const moveDialog = document.getElementById("move-dialog");
  const finishing = new Submitter(finishDialog, { label: t("finish") });
  const cancelling = new Submitter(cancelDialog, { label: t("cancel_booking") });
  const moving = new Submitter(moveDialog, { label: t("move") });
  const when = new When(moveDialog);
  let visit = null;
  [finishDialog, cancelDialog, moveDialog].forEach((d) => wireClose(d));

  async function done(dialog, submitter, result, message) {
    if (!result || !isOk(result)) {
      if (result && result.status === 409 && onChanged) onChanged();  // the row changed meanwhile: show it as it is
      return;
    }
    dialog.close();
    toast(message);
    if (onChanged) onChanged(result.data);
  }

  // --- finish ---
  const finishForm = finishDialog.querySelector("form");
  const owed = finishDialog.querySelector("[data-finish-owed]");
  async function finish(withPayment) {
    if (finishDialog.dataset.busy) return;
    let body = { status: visit.status };
    if (withPayment) {
      const amount = parsePiastres(finishForm.elements.namedItem("amount").value);
      if (!amount) { finishing.say(t("bad_amount")); return; }
      body.payment = { amount, method: finishForm.querySelector('[name="method"]:checked').value };
    }
    if (finishing.unknown) body = finishing.unknown.body;
    const result = await finishing.run("POST", `/api/v1/appointments/${visit.id}/finish`, body);
    owed.hidden = Boolean(finishing.unknown);  // while unknown, only the same request may be resent
    const paid = result && isOk(result) && body.payment;
    await done(finishDialog, finishing, result,
      paid ? t("finished_paid", { name: visit.name, amount: moneyText(body.payment.amount) }) : t("finished", { name: visit.name }));
  }
  finishForm.addEventListener("submit", (event) => { event.preventDefault(); finish(true); });
  owed.addEventListener("click", () => finish(false));

  // --- cancel ---
  const cancelForm = cancelDialog.querySelector("form");
  cancelForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = cancelling.unknown ? cancelling.unknown.body
      : { status: visit.status, reason: cancelForm.elements.namedItem("reason").value.trim() };
    const result = await cancelling.run("POST", `/api/v1/appointments/${visit.id}/cancel`, body);
    await done(cancelDialog, cancelling, result, t("cancelled", { name: visit.name }));
  });

  // --- move ---
  const moveForm = moveDialog.querySelector("form");
  moveForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    let body = moving.unknown ? moving.unknown.body : { status: visit.status, doctor_id: when.doctor(), day: when.day() };
    if (!moving.unknown) {
      if (!body.doctor_id || !body.day) { moving.say(t("need_doctor")); return; }
      if (when.slot()) { body.starts_at = when.slot(); body.extra = when.isExtra(); }
      else if (when.needsTime()) { moving.say(t("need_time")); return; }
    }
    const result = await moving.run("POST", `/api/v1/appointments/${visit.id}/reschedule`, body);
    await done(moveDialog, moving, result, t("moved", { name: visit.name }));
  });

  return {
    finish(v, opener) {
      visit = v;
      finishForm.reset();
      finishing.reset();
      owed.hidden = false;
      named(finishDialog, v.name);
      finishDialog.querySelector("[data-due-text]").textContent = t("owes", { amount: moneyText(v.due) });
      finishForm.elements.namedItem("amount").value = amountInput(v.due);
      openDialog(finishDialog, opener);
    },
    cancel(v, opener) {
      visit = v;
      cancelForm.reset();
      cancelling.reset();
      named(cancelDialog, v.name);
      openDialog(cancelDialog, opener);
    },
    move(v, opener) {
      visit = v;
      moveForm.reset();
      moving.reset();
      named(moveDialog, v.name);
      when.setDoctor(v.doctorId);
      when.setDay(v.day);
      openDialog(moveDialog, opener);
      when.refresh();
    },
  };
}
