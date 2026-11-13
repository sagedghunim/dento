// A follow-up row (web/_fu_row.html), shared by the Follow-ups list and Today's strip "just out of the visit".
// "Book" walks the Book dialog through the row's sessions; "Later" puts the row off. Each action keeps its request
// key until a definite answer, so a second tap or a resend never acts twice.
import { errorMessage, isOk, newKey, save } from "./api.js";
import { patientFrom } from "./book.js";
import { alertMessage, renderPending, toast } from "./shell.js";
import { t } from "./strings.js";

// The row's sessions still to book, soonest first: [{id, name, due}].
export function planOf(row) {
  return [...row.querySelectorAll("[data-item]")].map((li) => ({ id: Number(li.dataset.item), name: li.dataset.what, due: li.dataset.due }));
}

export function openPlan(book, row, opener) {
  const plan = planOf(row);
  if (!plan.length) return;
  book.open({ patient: patientFrom(row), doctorId: Number(row.dataset.doctor), sectionId: row.dataset.section, plan, opener });
}

// "Later". keepListed (Today's strip): the row only leaves the strip and stays in Follow-ups. True once it is done.
export async function later(row, keepListed) {
  if (row.dataset.busy) return false;
  row.dataset.busy = "1";
  row.actionKey = row.actionKey || newKey();
  row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  const body = { item_ids: planOf(row).map((item) => item.id) };
  if (keepListed) body.keep_listed = true;
  const result = await save("POST", "/api/v1/follow-ups/snooze", body, { key: row.actionKey, label: `${t("fu_later")} · ${row.dataset.name}` });
  delete row.dataset.busy;
  row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  if (!result.settled) { renderPending(); toast(t("unknown")); return false; }
  row.actionKey = null;
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return false; }
  toast(t(keepListed ? "fu_kept" : "fu_snoozed", { name: row.dataset.name }));
  return true;
}
