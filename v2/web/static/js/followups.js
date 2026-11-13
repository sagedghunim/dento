// Follow-ups (Saged, 2026-11-13): the desk's list of visits the doctor asked for that aren't booked yet.
// "Book" opens the Book dialog on the patient and the doctor and walks through the row's sessions, each on its
// suggested day; every booking carries its follow-up, so the server links the two in one request. A booked session
// leaves the row, and the row leaves the list with its last one. "Later" puts the row off for a week.
import { initBook } from "./book.js";
import { later, openPlan } from "./fu.js";
import { localDigits } from "./numbers.js";

const root = document.querySelector("[data-follow-ups]");

function leave(row) {
  const group = row.closest("[data-fu-group]");
  row.remove();
  const left = group.querySelectorAll("[data-fu]").length;
  if (left) group.querySelector("[data-fu-count]").textContent = localDigits(left);
  else group.remove();
  if (!root.querySelector("[data-fu]")) root.querySelector("[data-fu-empty]").hidden = false;
}

function booked(answer, session) {
  const item = session && root.querySelector(`[data-item="${session.id}"]`);
  if (!item) return;
  const row = item.closest("[data-fu]");
  item.remove();
  if (!row.querySelector("[data-item]")) leave(row);
}

const book = initBook({ onBooked: booked });

if (root) {
  root.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-fu]");
    if (!row) return;
    const opener = event.target.closest("[data-fu-book]");
    if (opener && book) openPlan(book, row, opener);
    if (event.target.closest("[data-fu-later]") && await later(row, false)) leave(row);
  });
}
