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
    const result = await finishing.run("POST", `/api/v1/appointments/${visit.id}/finish`, body, `${t("finish")} · ${visit.name}`);
    owed.hidden = Boolean(finishing.unknown);  // while unknown, only the same request may be resent
    if (!result) return;
    const error = result.data && result.data.error;
    if (result.status === 409 && error && error.code === "already_finished") {
      nothingCollected(error.appointment, error.message);  // someone finished it first: this cash wasn't recorded
      if (onChanged) onChanged();
      return;
    }
    if (isOk(result) && body.payment) {
      // "Collected" only from the answer: the invoice's paid must have risen by the amount (M1 decision R2).
      const invoice = result.data.appointment && result.data.appointment.invoice;
      const after = invoice ? invoice.paid : 0;
      if (after - visit.paid < body.payment.amount) { nothingCollected(result.data.appointment, t("nothing_collected")); if (onChanged) onChanged(); return; }
      await done(finishDialog, finishing, result, t("finished_paid", { name: visit.name, amount: moneyText(body.payment.amount) }));
      return;
    }
    await done(finishDialog, finishing, result, t("finished", { name: visit.name }));
  }

  // The visit is done and this payment wasn't taken: keep the dialog, say so, and offer the payment alone.
  function nothingCollected(appointment, message) {
    if (appointment) visit = { ...visit, status: appointment.status, due: appointment.due,
                               paid: appointment.invoice ? appointment.invoice.paid : 0 };
    finishing.say(message);
    finishDialog.querySelector("[data-submit]").hidden = true;
    owed.hidden = true;
    collectNow.hidden = !(visit.due > 0);
    finishDialog.querySelector("[data-due-text]").textContent = t("owes", { amount: moneyText(visit.due) });
    if (visit.due > 0) finishForm.elements.namedItem("amount").value = amountInput(visit.due);
  }

  const collectNow = finishDialog.querySelector("[data-collect-now]");
  collectNow.addEventListener("click", async () => {
    if (finishDialog.dataset.busy) return;
    let body = finishing.unknown ? finishing.unknown.body : null;
    if (!body) {
      const amount = parsePiastres(finishForm.elements.namedItem("amount").value);
      if (!amount) { finishing.say(t("bad_amount")); return; }
      body = { amount, method: finishForm.querySelector('[name="method"]:checked').value };
    }
    // A new request with its own key: the finish request's key belongs to the finish.
    const result = await finishing.run("POST", `/api/v1/appointments/${visit.id}/payments`, body,
      [t("payment"), visit.name, moneyText(body.amount)].join(" · "));
    if (result && isOk(result) && result.data.payment) {
      await done(finishDialog, finishing, result, t("collected", { amount: moneyText(body.amount), name: visit.name }));
    }
  });
  finishForm.addEventListener("submit", (event) => { event.preventDefault(); finish(true); });
  owed.addEventListener("click", () => finish(false));

  // --- cancel ---
  const cancelForm = cancelDialog.querySelector("form");
  cancelForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = cancelling.unknown ? cancelling.unknown.body
      : { status: visit.status, reason: cancelForm.elements.namedItem("reason").value.trim() };
    const result = await cancelling.run("POST", `/api/v1/appointments/${visit.id}/cancel`, body, `${t("cancel_booking")} · ${visit.name}`);
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
    const result = await moving.run("POST", `/api/v1/appointments/${visit.id}/reschedule`, body, `${t("move")} · ${visit.name}`);
    await done(moveDialog, moving, result, t("moved", { name: visit.name }));
  });

  return {
    finish(v, opener) {
      if (finishing.holdsUnknown()) { openDialog(finishDialog, opener); return; }
      visit = v;
      finishForm.reset();
      finishing.reset();
      owed.hidden = false;
      collectNow.hidden = true;
      finishDialog.querySelector("[data-submit]").hidden = false;
      named(finishDialog, v.name);
      finishDialog.querySelector("[data-due-text]").textContent = t("owes", { amount: moneyText(v.due) });
      finishForm.elements.namedItem("amount").value = amountInput(v.due);
      openDialog(finishDialog, opener);
    },
    cancel(v, opener) {
      if (cancelling.holdsUnknown()) { openDialog(cancelDialog, opener); return; }
      visit = v;
      cancelForm.reset();
      cancelling.reset();
      named(cancelDialog, v.name);
      openDialog(cancelDialog, opener);
    },
    move(v, opener) {
      if (moving.holdsUnknown()) { openDialog(moveDialog, opener); return; }
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
