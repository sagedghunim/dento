// The price list (Saged, 2026-11-22): the desk changes a price in its box (Enter or leaving the box saves it); the
// doctor adds a procedure under a main service, changes it, takes it off or brings it back. Each change is one
// request under its own key; the page reloads after a change to a procedure so every list shows it.
import { errorMessage, isOk, save } from "./api.js";
import { Submitter, openDialog, wireClose } from "./dialogs.js";
import { parsePiastres } from "./numbers.js";
import { toast } from "./shell.js";
import { t } from "./strings.js";

const list = document.querySelector("[data-price-list]");
const dialog = document.getElementById("procedure-dialog");
const procedureOf = (node) => JSON.parse(node.closest("[data-procedure]").dataset.procedure);

// --- search ---

const search = document.querySelector("[data-price-search]");
if (search && list) {
  search.addEventListener("input", () => {
    const q = search.value.trim().toLocaleLowerCase();
    for (const group of list.querySelectorAll("[data-group]")) {
      let any = false;
      for (const row of group.querySelectorAll("[data-find]")) {
        row.hidden = Boolean(q) && !row.dataset.find.toLocaleLowerCase().includes(q);
        any = any || !row.hidden;
      }
      group.hidden = !any;
    }
  });
}

// --- prices ---

async function savePrice(input) {
  const p = procedureOf(input);
  const price = parsePiastres(input.value);
  if (price == null || price < 0) { input.setCustomValidity(t("bad_amount")); input.reportValidity(); return; }
  input.setCustomValidity("");
  if (price === p.price) return;
  input.disabled = true;
  const result = await save("PATCH", `/api/v1/procedures/${p.id}`, { changes: { price } }, { label: p.name_ar });
  input.disabled = false;
  if (result.settled && result.status === 202) {  // waits for the owner's permission (Saged, 2026-11-25)
    input.value = input.defaultValue;
    toast(t("sent_for_permission"));
    return;
  }
  if (result.settled && isOk(result)) {
    p.price = price;
    input.closest("[data-procedure]").dataset.procedure = JSON.stringify(p);
    toast(t("saved"));
  } else {
    toast(errorMessage(result, t("failed")));
  }
}

if (list) {
  list.addEventListener("keydown", (event) => {
    if (event.target.matches("[data-price]") && event.key === "Enter") { event.preventDefault(); event.target.blur(); }
  });
  list.addEventListener("focusout", (event) => { if (event.target.matches("[data-price]")) savePrice(event.target); });
}

// --- procedures ---

async function setActive(button, active) {
  const p = procedureOf(button);
  button.disabled = true;
  const result = await save("PATCH", `/api/v1/procedures/${p.id}`, { changes: { is_active: active } }, { label: p.name_ar });
  if (result.settled && isOk(result)) { window.location.reload(); return; }
  button.disabled = false;
  toast(errorMessage(result, t("failed")));
}

document.addEventListener("click", (event) => {
  const off = event.target.closest("[data-take-off]");
  if (off) setActive(off, false);
  const back = event.target.closest("[data-bring-back]");
  if (back) setActive(back, true);
});

if (dialog) {
  const form = dialog.querySelector("form");
  const submitter = new Submitter(dialog, { label: t("saved") });
  const field = (name) => form.elements.namedItem(name);
  let editing = null;
  wireClose(dialog);

  function open(p, opener) {
    if (submitter.holdsUnknown()) { openDialog(dialog, opener); return; }
    editing = p;
    form.reset();
    submitter.reset();
    if (p) {
      field("name_ar").value = p.name_ar;
      field("name_en").value = p.name_en || "";
      field("family").value = p.family || "";
      if (field("family").value !== (p.family || "")) field("family").value = "";
      field("unit").value = p.unit;
      if (field("section_id")) { field("section_id").value = String(p.section_id); field("section_id").disabled = true; }
      if (field("price")) field("price").closest("label").hidden = true;  // the price is changed in the list
    } else {
      if (field("section_id")) field("section_id").disabled = false;
      if (field("price")) field("price").closest("label").hidden = false;
    }
    showNewFamily();
    openDialog(dialog, opener);
  }

  // A main service of the clinic's own (Saged, 2026-11-22): "+ A new main service" asks its name.
  const newFamily = dialog.querySelector("[data-family-new]");
  function showNewFamily() {
    newFamily.hidden = field("family").value !== "+new";
  }
  field("family").addEventListener("change", () => {
    showNewFamily();
    if (!newFamily.hidden) field("family_new").focus();
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-add-procedure]")) open(null, event.target.closest("button"));
    const edit = event.target.closest("[data-edit]");
    if (edit) open(procedureOf(edit), edit);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { name_ar: field("name_ar").value.trim(), name_en: field("name_en").value.trim(),
                   family: field("family").value, unit: field("unit").value };
    if (body.family === "+new") {
      body.family = field("family_new").value.trim().replace(/\s+/g, " ");
      if (!body.family) { submitter.say(t("family_needed")); field("family_new").focus(); return; }
    }
    if (!body.name_ar) { submitter.say(t("name_needed")); field("name_ar").focus(); return; }
    let result;
    if (editing) {
      result = await submitter.run("PATCH", `/api/v1/procedures/${editing.id}`, { changes: body }, body.name_ar);
    } else {
      if (field("section_id")) body.section_id = Number(field("section_id").value);
      if (field("price") && field("price").value.trim()) {
        const price = parsePiastres(field("price").value);
        if (price == null || price < 0) { submitter.say(t("bad_amount")); field("price").focus(); return; }
        body.price = price;
      }
      result = await submitter.run("POST", "/api/v1/procedures", body, body.name_ar);
    }
    if (result && result.status === 202) { submitter.dialog.close(); toast(t("sent_for_permission")); return; }
    if (result && isOk(result)) window.location.reload();
  });
}
