// Prescription (design E3): the medicines list and the ready prescriptions, each with add, edit and remove.
// Saves go through a dialog's Submitter (one request key until a definite answer); the lists come again from
// GET /prescriptions/medicines and /prescriptions/templates after each save.
import { errorMessage, fetchText, isOk, newKey, save } from "./api.js";
import { Submitter, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const medHolder = document.querySelector("[data-medicines]");
const rxHolder = document.querySelector("[data-templates]");
const filterInput = document.querySelector("[data-med-filter]");
const MAX_LINES = 30;

const medicines = () => Array.from(medHolder.querySelectorAll("[data-medicine]"), (n) => JSON.parse(n.dataset.medicine));

function applyFilter() {
  const needle = filterInput.value.trim().toLocaleLowerCase();
  let shown = 0;
  medHolder.querySelectorAll("[data-medicine]").forEach((row) => {
    row.hidden = Boolean(needle) && !row.dataset.search.toLocaleLowerCase().includes(needle);
    if (!row.hidden) shown += 1;
  });
  const none = medHolder.querySelector("[data-no-match]");
  if (none) none.hidden = shown > 0;
}
filterInput.addEventListener("input", applyFilter);

async function reload(which) {
  const holder = which === "medicines" ? medHolder : rxHolder;
  const result = await fetchText(`/prescriptions/${which}`);
  if (result.status !== 200) { alertMessage(errorMessage(result, t("failed"))); return; }
  holder.replaceChildren(parseFragment(result.text));
  if (which === "medicines") applyFilter();
}

const sectionOf = (form) => {
  const select = form.elements.namedItem("section_id");
  return select ? Number(select.value) : undefined;
};

function titles(dialog, editing) {
  dialog.querySelector("[data-title-new]").hidden = editing;
  dialog.querySelector("[data-title-edit]").hidden = !editing;
  dialog.querySelectorAll("[data-new-only]").forEach((n) => { n.hidden = editing; });
}

// --- a medicine ---

const MED_TEXT = ["name", "generic", "strength", "form", "default_dose", "default_note", "form_en", "default_dose_en", "default_note_en"];
const medDialog = document.getElementById("medicine-dialog");
const medForm = medDialog.querySelector("form");
const medSubmitter = new Submitter(medDialog);
let editingMed = null;
wireClose(medDialog);

// The editable (stored) text: form, dose and note come back in the page's language, with the raw ones beside them.
const rawMed = (m) => ({ ...m, form: m.form_ar, default_dose: m.default_dose_ar, default_note: m.default_note_ar });

function openMedicine(medicine, opener) {
  if (medSubmitter.holdsUnknown()) { openDialog(medDialog, opener); return; }
  editingMed = medicine ? rawMed(medicine) : null;
  medForm.reset();
  medSubmitter.reset();
  titles(medDialog, Boolean(medicine));
  const more = medDialog.querySelector("details");
  more.open = false;
  if (editingMed) {
    for (const name of MED_TEXT) medForm.elements.namedItem(name).value = editingMed[name] || "";
    more.open = Boolean(editingMed.form_en || editingMed.default_dose_en || editingMed.default_note_en);
  }
  openDialog(medDialog, opener);
}

medForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (medDialog.dataset.busy) return;
  let request = medSubmitter.unknown;
  if (!request) {
    const v = {};
    for (const name of MED_TEXT) v[name] = medForm.elements.namedItem(name).value.trim();
    if (!v.name) { medSubmitter.say(t("need_medicine_name")); return; }
    if (editingMed) {
      const changes = {};
      for (const name of MED_TEXT) if (v[name] !== (editingMed[name] || "")) changes[name] = v[name];
      if (!Object.keys(changes).length) { medDialog.close(); return; }
      request = { method: "PATCH", url: `/api/v1/medicines/${editingMed.id}`, body: { changes } };
    } else {
      const section = sectionOf(medForm);
      if (section) v.section_id = section;
      request = { method: "POST", url: "/api/v1/medicines", body: v };
    }
  }
  const name = (request.body.name || (request.body.changes && request.body.changes.name) || (editingMed && editingMed.name) || "");
  const result = await medSubmitter.run(request.method, request.url, request.body, t("medicine_saved", { name }));
  if (!result || !isOk(result)) return;
  medDialog.close();
  toast(t("medicine_saved", { name: result.data.medicine.name }));
  reload("medicines");
});

// --- a ready prescription ---

const rxDialog = document.getElementById("template-dialog");
const rxForm = rxDialog.querySelector("form");
const rxSubmitter = new Submitter(rxDialog);
const lines = rxDialog.querySelector("[data-lines]");
const lineTemplate = rxDialog.querySelector("[data-line-template]");
let editingRx = null;
wireClose(rxDialog);

function medicineOptions(select, keepId, keepName) {
  const [pick, other] = [select.options[0], select.options[1]];
  const list = medicines();
  const options = list.map((m) => {
    const option = document.createElement("option");
    option.value = m.id;
    option.textContent = m.strength ? `${m.name} ${m.strength}` : m.name;
    return option;
  });
  if (keepId && !list.some((m) => m.id === keepId)) {   // removed from the list since: still shown as it was saved
    const option = document.createElement("option");
    option.value = keepId;
    option.textContent = keepName;
    options.push(option);
  }
  select.replaceChildren(pick, ...options, other);
}

function addLine(line) {
  const node = lineTemplate.content.firstElementChild.cloneNode(true);
  const select = node.querySelector('[name="medicine_id"]');
  medicineOptions(select, line && line.medicine_id, line && line.name);
  if (line) {
    if (line.medicine_id) select.value = String(line.medicine_id);
    else { select.value = "other"; node.querySelector("[data-other]").hidden = false; node.querySelector('[name="other_name"]').value = line.name || ""; }
    node.querySelector('[name="dose"]').value = line.dose || "";
    node.querySelector('[name="note"]').value = line.note || "";
  }
  lines.append(node);
  return node;
}

lines.addEventListener("change", (event) => {
  const select = event.target.closest('[name="medicine_id"]');
  if (!select) return;
  const li = select.closest("li");
  li.querySelector("[data-other]").hidden = select.value !== "other";
  // Picking a medicine fills its usual dose and note, if the line has none yet.
  const medicine = medicines().find((m) => String(m.id) === select.value);
  if (medicine) {
    const dose = li.querySelector('[name="dose"]');
    const note = li.querySelector('[name="note"]');
    if (!dose.value) dose.value = medicine.default_dose_ar || "";
    if (!note.value) note.value = medicine.default_note_ar || "";
  }
  if (select.value === "other") li.querySelector('[name="other_name"]').focus();
});
lines.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove-line]");
  if (remove && !rxDialog.dataset.busy && !rxSubmitter.unknown) remove.closest("li").remove();
});
rxDialog.querySelector("[data-add-line]").addEventListener("click", () => {
  if (lines.children.length >= MAX_LINES || rxSubmitter.unknown) return;
  addLine(null).querySelector("select").focus();
});

function openTemplate(template, opener) {
  if (rxSubmitter.holdsUnknown()) { openDialog(rxDialog, opener); return; }
  editingRx = template;
  rxForm.reset();
  rxSubmitter.reset();
  titles(rxDialog, Boolean(template));
  lines.replaceChildren();
  const more = rxDialog.querySelector("details");
  more.open = false;
  if (template) {
    rxForm.elements.namedItem("name").value = template.name_ar || "";
    rxForm.elements.namedItem("notes").value = template.notes_ar || "";
    rxForm.elements.namedItem("name_en").value = template.name_en || "";
    rxForm.elements.namedItem("notes_en").value = template.notes_en || "";
    more.open = Boolean(template.name_en || template.notes_en);
    template.items.forEach(addLine);
  }
  if (!lines.children.length) addLine(null);
  openDialog(rxDialog, opener);
}

function readLines() {
  const out = [];
  for (const li of lines.children) {
    const choice = li.querySelector('[name="medicine_id"]').value;
    const dose = li.querySelector('[name="dose"]').value.trim();
    const note = li.querySelector('[name="note"]').value.trim();
    if (choice === "other") {
      const name = li.querySelector('[name="other_name"]').value.trim();
      if (!name) return null;
      out.push({ name, dose, note });
    } else if (choice) out.push({ medicine_id: Number(choice), dose, note });
    else return null;
  }
  return out;
}

rxForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (rxDialog.dataset.busy) return;
  let request = rxSubmitter.unknown;
  if (!request) {
    const name = rxForm.elements.namedItem("name").value.trim();
    if (!name) { rxSubmitter.say(t("need_rx_name")); return; }
    const items = readLines();
    if (!items || !items.length) { rxSubmitter.say(t("need_rx_lines")); return; }
    const body = { name, items, notes: rxForm.elements.namedItem("notes").value.trim(),
                   name_en: rxForm.elements.namedItem("name_en").value.trim(), notes_en: rxForm.elements.namedItem("notes_en").value.trim() };
    if (editingRx) {
      request = { method: "PUT", url: `/api/v1/prescription-templates/${editingRx.id}`, body: { ...body, is_active: true },
                  headers: { "If-Match": String(editingRx.version) } };
    } else {
      const section = sectionOf(rxForm);
      if (section) body.section_id = section;
      request = { method: "POST", url: "/api/v1/prescription-templates", body };
    }
  }
  const result = await rxSubmitter.run(request.method, request.url, request.body, t("rx_saved", { name: request.body.name }),
                                       { headers: request.headers });
  if (!result || !isOk(result)) return;
  rxDialog.close();
  toast(t("rx_saved", { name: result.data.template.name }));
  reload("templates");
});

// --- removing, with Undo ---

const removeDialog = document.getElementById("rx-remove-dialog");
const removeForm = removeDialog.querySelector("form");
const removeSubmitter = new Submitter(removeDialog);
let removing = null;   // {kind: "medicine" | "template", row}
wireClose(removeDialog);

function openRemove(kind, row, opener) {
  if (removeSubmitter.holdsUnknown()) { openDialog(removeDialog, opener); return; }
  removing = { kind, row };
  removeSubmitter.reset();
  removeDialog.querySelector("[data-name]").textContent = row.name;
  openDialog(removeDialog, opener);
}

async function undo(kind, row) {
  const request = kind === "medicine"
    ? ["PATCH", `/api/v1/medicines/${row.id}`, { changes: { is_active: true } }, undefined]
    : ["PUT", `/api/v1/prescription-templates/${row.id}`,
       { name: row.name_ar, notes: row.notes_ar, name_en: row.name_en, notes_en: row.notes_en, is_active: true,
         items: row.items.map((l) => ({ medicine_id: l.medicine_id, name: l.name, strength: l.strength, form: l.form, dose: l.dose, note: l.note })) },
       { "If-Match": String(row.version) }];
  const result = await save(request[0], request[1], request[2], { key: newKey(), label: row.name, headers: request[3] });
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { alertMessage(t("unknown")); return; }
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
  toast(t("restored", { name: row.name }));
  reload(kind === "medicine" ? "medicines" : "templates");
}

removeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (removeDialog.dataset.busy || !removing) return;
  const { kind, row } = removing;
  const url = kind === "medicine" ? `/api/v1/medicines/${row.id}` : `/api/v1/prescription-templates/${row.id}`;
  const result = await removeSubmitter.run("DELETE", url, undefined, t("removed", { name: row.name }));
  if (!result || !isOk(result)) return;
  removeDialog.close();
  // A template's version moved on with the removal: Undo replaces that version.
  const after = kind === "template" ? { ...row, version: result.data.template.version } : row;
  toast(t("removed", { name: row.name }), { label: t("undo"), onClick: () => undo(kind, after) });
  reload(kind === "medicine" ? "medicines" : "templates");
});

// --- one listener for the buttons ---

document.addEventListener("click", (event) => {
  const newMed = event.target.closest("[data-new-medicine]");
  if (newMed) { openMedicine(null, newMed); return; }
  const newRx = event.target.closest("[data-new-template]");
  if (newRx) { openTemplate(null, newRx); return; }
  const med = event.target.closest("[data-med]");
  if (med) {
    const row = JSON.parse(med.closest("[data-medicine]").dataset.medicine);
    if (med.dataset.med === "edit") openMedicine(row, med); else openRemove("medicine", row, med);
    return;
  }
  const rx = event.target.closest("[data-rx]");
  if (rx) {
    const row = JSON.parse(rx.closest("[data-template]").dataset.template);
    if (rx.dataset.rx === "edit") openTemplate(row, rx); else openRemove("template", row, rx);
  }
});
