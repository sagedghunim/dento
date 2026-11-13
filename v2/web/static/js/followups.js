// Follow-ups (Saged, 2026-11-13): the desk's list of visits the doctor asked for that aren't booked yet.
// "Book" opens the Book dialog on the patient and the doctor (and the suggested day, once the dialog takes one);
// when the booking answers, the follow-up is linked to it and the row leaves the list. "Later" puts the row off for
// a week. Each action keeps its request key until a definite answer, so a second tap or a resend never acts twice.
import { errorMessage, isOk, newKey, save } from "./api.js";
import { initBook, patientFrom } from "./book.js";
import { alertMessage, renderPending, toast } from "./shell.js";
import { localDigits } from "./numbers.js";
import { t } from "./strings.js";

const root = document.querySelector("[data-follow-ups]");
let booking = null;  // the row whose Book dialog is open

function leave(row) {
  const group = row.closest("[data-fu-group]");
  row.remove();
  const left = group.querySelectorAll("[data-fu]").length;
  if (left) group.querySelector("[data-fu-count]").textContent = localDigits(left);
  else group.remove();
  if (!root.querySelector("[data-fu]")) root.querySelector("[data-fu-empty]").hidden = false;
}

const ids = (row) => row.dataset.items.split(",").map(Number);

async function act(row, url, body, label) {
  if (row.dataset.busy) return false;
  row.dataset.busy = "1";
  row.actionKey = row.actionKey || newKey();
  row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  const result = await save("POST", url, body, { key: row.actionKey, label });
  delete row.dataset.busy;
  row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  if (!result.settled) { renderPending(); toast(t("unknown")); return false; }
  row.actionKey = null;
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return false; }
  return true;
}

// The first follow-up of the row is the one this booking is for; the rest stay for their own bookings.
async function linkBooked(row, appointment) {
  const [first, ...rest] = ids(row);
  const ok = await act(row, "/api/v1/follow-ups/booked", { item_ids: [first], appointment_id: appointment.id },
    `${t("fu_title")} · ${row.dataset.name}`);
  if (!ok) return;
  if (!rest.length) { leave(row); return; }
  row.dataset.items = rest.join(",");
  row.querySelector(".fu-items li").remove();
}

const book = initBook({ onBooked: (answer) => { const row = booking; booking = null; if (row && row.isConnected) linkBooked(row, answer.appointment); } });

if (root) {
  root.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-fu]");
    if (!row) return;
    const opener = event.target.closest("[data-fu-book]");
    if (opener && book) {
      booking = row;
      book.open({ patient: patientFrom(row), doctorId: Number(row.dataset.doctor), sectionId: row.dataset.section,
                  day: row.dataset.day, opener });
    }
    if (event.target.closest("[data-fu-later]")) {
      if (await act(row, "/api/v1/follow-ups/snooze", { item_ids: ids(row) }, `${t("fu_later")} · ${row.dataset.name}`)) {
        leave(row);
        toast(t("fu_snoozed", { name: row.dataset.name }));
      }
    }
  });
}
