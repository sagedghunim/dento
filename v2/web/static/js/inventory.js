// Inventory (design F2): items, stock in and out, history and what each procedure uses.
// Every save goes through a dialog's Submitter: one request key until a definite answer, so a double tap or a
// dropped connection never records stock twice. After a save the cards come again from GET /inventory/items.
import { errorMessage, fetchText, isOk, newKey, save } from "./api.js";
import { Submitter, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { amountInput, asciiDigits, localDigits, moneyText, parsePiastres, parseWhole } from "./numbers.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const holder = document.querySelector("[data-items]");
const filterInput = document.querySelector("[data-filter]");
const lowButton = document.querySelector("[data-low-only]");
const removedButton = document.querySelector("[data-removed]");
let showRemoved = false;

const itemOf = (node) => JSON.parse(node.closest("[data-item]").dataset.item);
const stockText = (item) => t("inv_stock", { n: localDigits(item.quantity), unit: item.unit });

// --- the list: filter as the person types, low stock only, removed items ---

function applyFilter() {
  const needle = (filterInput ? filterInput.value : "").trim().toLocaleLowerCase();
  const lowOnly = lowButton && lowButton.getAttribute("aria-pressed") === "true";
  let shown = 0;
  holder.querySelectorAll("[data-item]").forEach((card) => {
    const hit = (!needle || card.dataset.search.toLocaleLowerCase().includes(needle)) && (!lowOnly || card.dataset.low === "1");
    card.hidden = !hit;
    if (hit) shown += 1;
  });
  const none = holder.querySelector("[data-no-match]");
  if (none) none.hidden = shown > 0;
}

async function reload() {
  const result = await fetchText(`/inventory/items${showRemoved ? "?removed=1" : ""}`);
  if (result.status !== 200) { alertMessage(errorMessage(result, t("failed"))); return; }
  holder.replaceChildren(parseFragment(result.text));
  applyFilter();
}

if (filterInput) filterInput.addEventListener("input", applyFilter);
if (lowButton) lowButton.addEventListener("click", () => {
  lowButton.setAttribute("aria-pressed", String(lowButton.getAttribute("aria-pressed") !== "true"));
  applyFilter();
});
if (removedButton) removedButton.addEventListener("click", () => {
  showRemoved = !showRemoved;
  removedButton.setAttribute("aria-pressed", String(showRemoved));
  reload();
});

// --- one small dialog per action ---

function setup(id, build) {
  const dialog = document.getElementById(id);
  if (!dialog) return null;
  const form = dialog.querySelector("form");
  const submitter = new Submitter(dialog);
  wireClose(dialog);
  let current = null;
  const field = (name) => form.elements.namedItem(name);
  const spec = build({ dialog, form, field, submitter, get item() { return current; } });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;
    let request;
    if (submitter.unknown) request = submitter.unknown;
    else {
      request = spec.request(current);
      if (!request) return;            // the form said what is wrong
    }
    const result = await submitter.run(request.method, request.url, request.body, request.label, { headers: request.headers });
    if (!result || !isOk(result)) return;
    dialog.close();
    spec.done(result.data, current);
  });
  return {
    open(item, opener) {
      if (submitter.holdsUnknown()) { openDialog(dialog, opener); return; }
      current = item;
      form.reset();
      submitter.reset();
      dialog.querySelectorAll("[data-name]").forEach((n) => { n.textContent = item ? item.name : ""; });
      dialog.querySelectorAll("[data-stock]").forEach((n) => { n.textContent = item ? stockText(item) : ""; });
      if (spec.fill) spec.fill(item);
      openDialog(dialog, opener);
    },
  };
}

const whole = (submitter, value, min = 1) => {
  const n = parseWhole(value);
  if (n === null || n < min) { submitter.say(t("bad_qty")); return null; }
  return n;
};

// Use: stock out for a patient or the clinic.
let pickedPatient = null;
const useDialog = setup("use-dialog", ({ dialog, field, submitter }) => {
  const pick = dialog.querySelector("[data-patient-pick]");
  if (pick) wirePatientPick(pick);
  return {
    fill() { pickedPatient = null; if (pick) showPicked(pick); },
    request(item) {
      const qty = whole(submitter, field("qty").value);
      if (qty === null) return null;
      const body = { lines: [{ item_id: item.id, qty }], reason: field("reason").value.trim() };
      if (pickedPatient) body.patient_id = pickedPatient.id;
      return { method: "POST", url: "/api/v1/inventory/uses", body, label: t("inv_used", { qty: localDigits(qty), name: item.name }) };
    },
    done(data, item) {
      const after = data.items && data.items[0];
      toast(after ? t("inv_used_left", { name: item.name, n: localDigits(after.quantity), unit: after.unit }) : t("saved"));
      reload();
    },
  };
});

function showPicked(pick) {
  const picked = pick.querySelector("[data-picked]");
  const q = pick.querySelector("[data-patient-q]");
  picked.hidden = !pickedPatient;
  q.hidden = Boolean(pickedPatient);
  pick.querySelector("[data-picked-name]").textContent = pickedPatient ? pickedPatient.name : "";
  if (!pickedPatient) { q.value = ""; pick.querySelector("[data-patient-results]").replaceChildren(); }
}

function wirePatientPick(pick) {
  const q = pick.querySelector("[data-patient-q]");
  const results = pick.querySelector("[data-patient-results]");
  let timer = null;
  let controller = null;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const text = q.value.trim();
      if (controller) controller.abort();
      if (text.length < 2) { results.replaceChildren(); return; }
      controller = new AbortController();
      const result = await fetchText(`/patients/search?pick=1&q=${encodeURIComponent(text)}`, { signal: controller.signal });
      if (result.aborted) return;
      if (result.status !== 200) { results.textContent = t("search_failed"); return; }
      results.replaceChildren(parseFragment(result.text));
    }, 250);
  });
  results.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pick-patient]");
    if (!button) return;
    pickedPatient = { id: Number(button.dataset.id), name: button.dataset.name };
    showPicked(pick);
  });
  pick.querySelector("[data-unpick]").addEventListener("click", () => { pickedPatient = null; showPicked(pick); q.focus(); });
}

// Purchase: stock in, the average cost and (by default) an expense.
const purchaseDialog = setup("purchase-dialog", ({ dialog, field, submitter }) => {
  const total = dialog.querySelector("[data-total]");
  const shiftWrap = dialog.querySelector("[data-shift-wrap]");
  const sum = () => {
    const qty = parseWhole(field("qty").value);
    const price = parsePiastres(field("unit_price").value);
    total.textContent = qty && price !== null ? t("inv_total", { amount: moneyText(qty * price) }) : "";
    shiftWrap.hidden = !field("record_expense").checked;
  };
  dialog.addEventListener("input", sum);
  dialog.addEventListener("change", sum);
  return {
    fill(item) { field("unit_price").value = item.unit_cost ? amountInput(item.unit_cost) : ""; sum(); },
    request(item) {
      const qty = whole(submitter, field("qty").value);
      if (qty === null) return null;
      const price = parsePiastres(field("unit_price").value || "0");
      if (price === null) { submitter.say(t("bad_amount")); return null; }
      const record = field("record_expense").checked;
      return { method: "POST", url: `/api/v1/inventory/items/${item.id}/purchases`, label: t("inv_bought", { qty: localDigits(qty), name: item.name }),
               body: { qty, unit_price: price, record_expense: record, from_shift: record && field("from_shift").checked, note: field("note").value.trim() } };
    },
    done(data, item) { toast(t("inv_bought_left", { name: item.name, n: localDigits(data.item.quantity), unit: data.item.unit })); reload(); },
  };
});

// Count: set the quantity on the shelf, against the quantity the person saw.
const countDialog = setup("count-dialog", ({ field, submitter }) => ({
  fill(item) { field("counted").value = String(item.quantity); },
  request(item) {
    const raw = asciiDigits(field("counted").value).trim();
    const counted = /^-?\d+$/.test(raw) ? Number(raw) : null;
    if (counted === null) { submitter.say(t("bad_qty")); return null; }
    return { method: "POST", url: `/api/v1/inventory/items/${item.id}/count`, label: t("inv_counted", { name: item.name }),
             body: { counted, expected: item.quantity, reason: field("reason").value.trim() } };
  },
  done(data, item) { toast(t("inv_counted", { name: item.name })); reload(); },
}));

// Waste: expired or broken stock out, with a reason.
const wasteDialog = setup("waste-dialog", ({ field, submitter }) => ({
  request(item) {
    const qty = whole(submitter, field("qty").value);
    if (qty === null) return null;
    const reason = field("reason").value.trim();
    if (!reason) { submitter.say(t("need_reason")); return null; }
    return { method: "POST", url: `/api/v1/inventory/items/${item.id}/waste`, body: { qty, reason }, label: t("inv_wasted", { name: item.name }) };
  },
  done(data, item) { toast(t("inv_wasted", { name: item.name })); reload(); },
}));

// Add or edit an item. Editing sends only what changed.
const TEXT = ["name", "name_en", "unit", "category", "sku", "notes"];
const itemDialog = setup("item-dialog", ({ dialog, form, field, submitter }) => ({
  fill(item) {
    dialog.querySelector("[data-title-new]").hidden = Boolean(item);
    dialog.querySelector("[data-title-edit]").hidden = !item;
    dialog.querySelectorAll("[data-new-only]").forEach((n) => { n.hidden = Boolean(item); });
    dialog.querySelector("details").open = false;
    if (!item) return;
    for (const name of TEXT) field(name).value = name === "name" ? item.name_ar : (item[name] || "");
    field("threshold").value = String(item.threshold);
    if (field("unit_cost")) field("unit_cost").value = item.unit_cost ? amountInput(item.unit_cost) : "";
    field("allow_negative").checked = item.allow_negative;
    if (field("section_id")) field("section_id").value = item.section_id || "";
    if (item.name_en || item.category || item.sku || item.notes || item.allow_negative || item.section_id) dialog.querySelector("details").open = true;
  },
  request(item) {
    const v = {};
    for (const name of TEXT) v[name] = field(name).value.trim();
    if (v.name.length < 1) { submitter.say(t("need_item_name")); return null; }
    v.threshold = parseWhole(field("threshold").value || "0");
    if (v.threshold === null) { submitter.say(t("bad_qty")); return null; }
    if (field("unit_cost")) {  // only for those who see money (Saged, 2026-11-16)
      v.unit_cost = parsePiastres(field("unit_cost").value || "0");
      if (v.unit_cost === null) { submitter.say(t("bad_amount")); return null; }
    }
    v.allow_negative = field("allow_negative").checked;
    if (field("section_id")) v.section_id = field("section_id").value ? Number(field("section_id").value) : null;
    if (!item) {
      v.quantity = parseWhole(field("quantity").value || "0");
      if (v.quantity === null) { submitter.say(t("bad_qty")); return null; }
      return { method: "POST", url: "/api/v1/inventory/items", body: v, label: t("inv_item_saved", { name: v.name }) };
    }
    const before = { ...item, name: item.name_ar };
    const changes = {};
    for (const [name, value] of Object.entries(v)) if ((value ?? "") !== (before[name] ?? "")) changes[name] = value;
    if (!Object.keys(changes).length) { form.closest("dialog").close(); return null; }
    return { method: "PATCH", url: `/api/v1/inventory/items/${item.id}`, body: changes, label: t("inv_item_saved", { name: v.name }) };
  },
  done(data) { toast(t("inv_item_saved", { name: data.item.name })); reload(); },
}));

// Remove (deactivate), with Undo; restore from Removed items.
async function setActive(item, active) {
  const result = await save("PATCH", `/api/v1/inventory/items/${item.id}`, { is_active: active }, { key: newKey(), label: item.name });
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { alertMessage(t("unknown")); return false; }
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return false; }
  return true;
}

const removeDialog = setup("remove-dialog", () => ({
  request(item) {
    return { method: "PATCH", url: `/api/v1/inventory/items/${item.id}`, body: { is_active: false }, label: t("inv_removed", { name: item.name }) };
  },
  done(data, item) {
    toast(t("inv_removed", { name: item.name }), { label: t("undo"), onClick: async () => { if (await setActive(item, true)) { toast(t("inv_restored", { name: item.name })); reload(); } } });
    reload();
  },
}));

// --- an item's history, newest first, with "Load more" ---

const historyDialog = document.getElementById("history-dialog");
let historyItem = null;
if (historyDialog) wireClose(historyDialog);

async function loadMoves(after) {
  const list = historyDialog.querySelector("[data-moves]");
  const status = historyDialog.querySelector("[data-moves-status]");
  const more = historyDialog.querySelector("[data-moves-more]");
  const item = historyItem;
  more.disabled = true;
  status.textContent = t("loading");
  const result = await fetchText(`/inventory/items/${item.id}/movements${after ? `?after=${encodeURIComponent(after)}` : ""}`);
  if (historyItem !== item) return;
  more.disabled = false;
  if (result.status !== 200) { status.textContent = errorMessage(result, t("history_failed")); return; }
  status.textContent = "";
  list.append(parseFragment(result.text));
  const next = result.headers.get("X-Next");
  more.hidden = !next;
  more.dataset.next = next || "";
}

function openHistory(item, opener) {
  historyItem = item;
  historyDialog.querySelector("[data-name]").textContent = item.name;
  historyDialog.querySelector("[data-moves]").replaceChildren();
  historyDialog.querySelector("[data-moves-more]").hidden = true;
  openDialog(historyDialog, opener);
  loadMoves("");
}
if (historyDialog) historyDialog.querySelector("[data-moves-more]").addEventListener("click", (e) => loadMoves(e.currentTarget.dataset.next));

// --- what each procedure uses ---

const materialsDialog = document.getElementById("materials-dialog");
let materialsRow = null;
if (materialsDialog) {
  const form = materialsDialog.querySelector("form");
  const lines = materialsDialog.querySelector("[data-lines]");
  const template = materialsDialog.querySelector("[data-line-template]");
  const submitter = new Submitter(materialsDialog);
  wireClose(materialsDialog);

  // The item choices follow the cards on the page, so an item added a minute ago is offered too.
  function options(select) {
    const cards = showRemoved ? [] : Array.from(holder.querySelectorAll("[data-item]"), (c) => JSON.parse(c.dataset.item));
    if (!cards.length) return;
    const first = select.options[0];
    select.replaceChildren(first, ...cards.filter((i) => i.is_active).map((i) => {
      const option = document.createElement("option");
      option.value = i.id;
      option.textContent = i.unit ? `${i.name} (${i.unit})` : i.name;
      return option;
    }));
  }

  function addLine(itemId, qty) {
    const node = template.content.firstElementChild.cloneNode(true);
    const select = node.querySelector("select");
    options(select);
    if (itemId) {
      if (!select.querySelector(`option[value="${Number(itemId)}"]`)) {
        const option = document.createElement("option");
        option.value = itemId;
        option.textContent = t("inv_unknown_item");
        select.append(option);
      }
      select.value = String(itemId);
    }
    node.querySelector('[name="qty"]').value = String(qty || 1);
    lines.append(node);
    return node;
  }

  materialsDialog.querySelector("[data-add-line]").addEventListener("click", () => {
    if (lines.children.length >= 30) return;
    addLine(null, 1).querySelector("select").focus();
  });
  lines.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-line]");
    if (remove && !materialsDialog.dataset.busy && !submitter.unknown) remove.closest("li").remove();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (materialsDialog.dataset.busy) return;
    const procedure = JSON.parse(materialsRow.dataset.procedure);
    let body = null;
    if (!submitter.unknown) {
      const picked = [];
      for (const li of lines.children) {
        const itemId = Number(li.querySelector("select").value);
        const qty = parseWhole(li.querySelector('[name="qty"]').value);
        if (!itemId) { submitter.say(t("need_line_item")); return; }
        if (!qty || qty > 10000) { submitter.say(t("bad_qty")); return; }
        if (picked.some((l) => l.item_id === itemId)) { submitter.say(t("line_twice")); return; }
        picked.push({ item_id: itemId, qty });
      }
      body = { lines: picked };
    }
    const result = await submitter.run("PUT", `/api/v1/inventory/materials/${procedure.id}`, body, t("materials_saved", { name: procedure.name }));
    if (!result || !isOk(result)) return;
    materialsDialog.close();
    const items = result.data.items;
    materialsRow.dataset.procedure = JSON.stringify({ ...procedure, lines: items.map((i) => ({ item_id: i.item_id, qty: i.qty })) });
    const text = materialsRow.querySelector("[data-mat-text]");
    text.textContent = items.length ? items.map((i) => `${i.name} × ${localDigits(i.qty)}`).join(" · ") : t("nothing_taken");
    toast(t("materials_saved", { name: procedure.name }));
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-materials]");
    if (!button) return;
    if (submitter.holdsUnknown()) { openDialog(materialsDialog, button); return; }
    materialsRow = button.closest("[data-procedure]");
    const procedure = JSON.parse(materialsRow.dataset.procedure);
    submitter.reset();
    materialsDialog.querySelector("[data-name]").textContent = procedure.name;
    lines.replaceChildren();
    for (const line of procedure.lines) addLine(line.item_id, line.qty);
    if (!procedure.lines.length) addLine(null, 1);
    openDialog(materialsDialog, button);
  });
}

// --- the starter list for the specialty ---

async function addDefaults(button) {
  if (button.disabled) return;
  button.disabled = true;
  const result = await save("POST", "/api/v1/inventory/defaults", {}, { key: newKey(), label: button.textContent.trim() });
  button.disabled = false;
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { alertMessage(t("unknown")); return; }
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
  toast(t("inv_defaults"));
  reload();
}

// --- one listener for every card button ---

const actions = {
  use: useDialog, purchase: purchaseDialog, count: countDialog, waste: wasteDialog, edit: itemDialog, remove: removeDialog,
};

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-inv]");
  if (button) {
    const menu = button.closest("details");
    if (menu) menu.open = false;
    const item = itemOf(button);
    const kind = button.dataset.inv;
    const opener = menu ? menu.querySelector("summary") : button;
    if (kind === "history") openHistory(item, opener);
    else if (kind === "restore") {
      button.disabled = true;
      if (await setActive(item, true)) { toast(t("inv_restored", { name: item.name })); reload(); }
      button.disabled = false;
    } else if (actions[kind]) actions[kind].open(item, opener);
    return;
  }
  if (event.target.closest("[data-show-low]") && lowButton) {
    lowButton.setAttribute("aria-pressed", "true");
    applyFilter();
    lowButton.scrollIntoView({ block: "nearest" });
    return;
  }
  const add = event.target.closest("[data-new-item]");
  if (add && itemDialog) { itemDialog.open(null, add); return; }
  const defaults = event.target.closest("[data-defaults]");
  if (defaults) addDefaults(defaults);
});
