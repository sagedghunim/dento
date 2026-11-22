// Finances (design F1, F3, F4, F5): the register, expenses, categories, pay rules and payouts.
// Every save goes through a dialog's Submitter: one request key until a definite answer, so a double tap or a
// dropped connection never records money twice. After a save the tab comes again from GET /finances/part.
import { errorMessage, fetchText, isOk, newKey, save } from "./api.js";
import { Submitter, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { amountInput, asciiDigits, moneyText, parsePiastres } from "./numbers.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const body = document.querySelector("[data-tab-body]");
const tab = body ? body.dataset.tab : "";
const month = body ? body.dataset.month : "";

// --- the tab: again after a save, and when the person comes back to the page ---

let reloading = null;
async function reloadTab() {
  if (reloading) return reloading;
  reloading = (async () => {
    const result = await fetchText(`/finances/part?tab=${encodeURIComponent(tab)}&month=${encodeURIComponent(month)}`);
    if (result.status !== 200) { alertMessage(errorMessage(result, t("failed"))); return; }
    body.replaceChildren(parseFragment(result.text));
  })();
  try { await reloading; } finally { reloading = null; }
}

// The register changes when the front desk takes cash: look again when the tab comes back into view.
// No polling: a hidden tab asks nothing.
let lastLoad = Date.now();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || tab !== "register") return;
  if (Date.now() - lastLoad < 15000 || document.querySelector("dialog[open]")) return;
  lastLoad = Date.now();
  reloadTab();
});

// The month picker moves as soon as a month is chosen.
const pick = document.querySelector("[data-month-pick]");
if (pick) pick.addEventListener("change", () => pick.form.submit());

// --- one small dialog per action (the inventory screen's pattern) ---

function setup(id, build) {
  const dialog = document.getElementById(id);
  if (!dialog) return null;
  const form = dialog.querySelector("form");
  const submitter = new Submitter(dialog);
  wireClose(dialog);
  let current = null;
  const field = (name) => form.elements.namedItem(name);
  const spec = build({ dialog, form, field, submitter });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;
    let request;
    if (submitter.unknown) request = submitter.unknown;
    else {
      request = spec.request(current);
      if (!request) return;            // the form said what is wrong
    }
    const result = await submitter.run(request.method, request.url, request.body, request.label);
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
      dialog.querySelectorAll("[data-name]").forEach((n) => { n.textContent = item && item.name ? item.name : ""; });
      if (spec.fill) spec.fill(item);
      openDialog(dialog, opener);
    },
  };
}

function money(submitter, value, { zero = false } = {}) {
  const p = parsePiastres(value);
  if (p === null || (!zero && p < 1)) { submitter.say(t("bad_amount")); return null; }
  return p;
}

const dataOf = (node, name) => JSON.parse(node.closest(`[data-${name}]`).dataset[name]);

// --- the register ---

// "Other drawer movement": the rare cash that is neither a patient's payment nor an expense. Needs a reason.
const cashDialog = setup("cash-dialog", ({ dialog, field, submitter }) => {
  return {
    fill(item) {
      dialog.querySelector(`[name="kind"][value="${item.kind}"]`).checked = true;
    },
    request() {
      const kind = dialog.querySelector('[name="kind"]:checked').value;
      const amount = money(submitter, field("amount").value);
      if (amount === null) return null;
      const reason = field("reason").value.trim();
      if (!reason) { submitter.say(t("need_reason")); return null; }
      return { method: "POST", url: "/api/v1/register/movements", body: { kind, amount, reason },
               label: t(kind === "in" ? "fin_cash_in" : "fin_cash_out", { amount: moneyText(amount) }) };
    },
    done(data) {
      toast(t("fin_cash_saved", { expected: moneyText(data.totals.expected_cash) }));
      reloadTab();
    },
  };
});

const openShiftDialog = setup("open-dialog", ({ field, submitter }) => ({
  request() {
    const amount = money(submitter, field("amount").value, { zero: true });
    if (amount === null) return null;
    return { method: "POST", url: "/api/v1/register/open", body: { opening_float: amount, note: field("note").value.trim() },
             label: t("fin_opened") };
  },
  done() { toast(t("fin_opened")); reloadTab(); },
}));

const closeShiftDialog = setup("close-dialog", ({ dialog, field, submitter }) => ({
  fill(item) { dialog.querySelector("[data-expected-text]").textContent = moneyText(item.expected); },
  request(item) {
    const counted = money(submitter, field("amount").value, { zero: true });
    if (counted === null) return null;
    return { method: "POST", url: `/api/v1/register/shifts/${item.id}/close`, body: { counted, note: field("note").value.trim() },
             label: t("fin_closed_label") };
  },
  done(data) {
    const diff = data.shift.difference;
    toast(diff === 0 ? t("fin_closed_match") : t(diff < 0 ? "fin_closed_short" : "fin_closed_over", { amount: moneyText(Math.abs(diff)) }));
    reloadTab();
  },
}));

const reportDialog = document.getElementById("report-dialog");
if (reportDialog) wireClose(reportDialog);
async function openReport(id, opener) {
  const holder = reportDialog.querySelector("[data-report-body]");
  holder.textContent = t("loading");
  openDialog(reportDialog, opener);
  const result = await fetchText(`/finances/shifts/${encodeURIComponent(id)}`);
  if (result.status !== 200) { holder.textContent = errorMessage(result, t("failed")); return; }
  holder.replaceChildren(parseFragment(result.text));
}

// --- expenses and categories ---

// "Pay an expense" (register and expenses tabs): tap what it was for, type the amount, save. Only "Other" (money
// that fits no category) needs a note; the server checks that too. From the register it comes out of the drawer.
const expenseDialog = setup("expense-dialog", ({ dialog, field, submitter }) => {
  const chips = dialog.querySelector("[data-category-chips]");
  const picked = () => dialog.querySelector('[name="category_id"]:checked');
  const isOther = () => Boolean(picked() && picked().dataset.code === "other");
  const noteLabel = () => {
    dialog.querySelector("[data-note-optional]").hidden = isOther();
    dialog.querySelector("[data-note-required]").hidden = !isOther();
    field("note").required = isOther();
  };
  chips.addEventListener("change", () => { noteLabel(); field("amount").focus(); });
  return {
    fill(item) {
      const day = field("day");
      if (day) day.value = day.dataset.today;
      // On the expenses tab the choices follow the categories listed there, so one added a minute ago is offered.
      const listed = Array.from(body.querySelectorAll(".cat-list [data-category]"), (row) => JSON.parse(row.dataset.category));
      if (listed.length) {
        listed.sort((a, b) => (a.code === "other") - (b.code === "other"));
        chips.replaceChildren(...listed.map((c) => {
          const label = document.createElement("label");
          label.className = "chip";
          const input = document.createElement("input");
          Object.assign(input, { type: "radio", name: "category_id", value: String(c.id) });
          input.dataset.code = c.code || "";
          const text = document.createElement("span");
          const name = document.createElement("bdi");
          name.dir = "auto";
          name.textContent = c.name;
          text.append(name);
          label.append(input, text);
          return label;
        }));
      }
      const fromShift = field("from_shift");
      if (fromShift) fromShift.checked = Boolean(item && item.fromDrawer);
      noteLabel();
    },
    request() {
      const chosen = picked();
      if (!chosen) { submitter.say(t("fin_need_category")); return null; }
      const amount = money(submitter, field("amount").value);
      if (amount === null) return null;
      const note = field("note").value.trim();
      if (isOther() && !note) { submitter.say(t("need_reason")); field("note").focus(); return null; }
      const day = field("day") ? asciiDigits(field("day").value) : "";
      if (day && !/^\d{4}-\d{2}-\d{2}$/.test(day)) { submitter.say(t("fin_bad_day")); return null; }
      const fromShift = field("from_shift");
      const name = chosen.closest(".chip").textContent.trim();
      if (dialog.hasAttribute("data-drawer-only")) {  // the desk: today, out of the drawer (Saged, 2026-11-22)
        return { method: "POST", url: "/api/v1/drawer-expenses", body: { category_id: Number(chosen.value), amount, note },
                 label: t("fin_expense_added", { name, amount: moneyText(amount) }) };
      }
      return { method: "POST", url: "/api/v1/expenses",
               body: { category_id: Number(chosen.value), amount, day: day || undefined, note, from_shift: Boolean(fromShift && fromShift.checked) },
               label: t("fin_expense_added", { name, amount: moneyText(amount) }) };
    },
    done(data) {
      const e = data.expense;
      toast(tab !== "expenses" || e.day.slice(0, 7) === month ? t("fin_expense_saved") : t("fin_expense_other_month"));
      reloadTab();
    },
  };
});

const voidDialog = setup("void-dialog", ({ dialog }) => ({
  fill(item) {
    dialog.querySelector("[data-name]").textContent = item.category;
    dialog.querySelector("[data-amount]").textContent = moneyText(item.amount);
  },
  request(item) {
    return { method: "POST", url: `/api/v1/expenses/${item.id}/void`, body: {}, label: t("fin_voided", { name: item.category }) };
  },
  done(_data, item) { toast(t("fin_voided", { name: item.category })); reloadTab(); },
}));

const categoryDialog = setup("category-dialog", ({ dialog, field, submitter }) => ({
  fill(item) {
    dialog.querySelector("[data-title-new]").hidden = Boolean(item);
    dialog.querySelector("[data-title-edit]").hidden = !item;
    field("name").value = item ? item.name_ar || "" : "";
    field("name_en").value = item ? item.name_en || "" : "";
  },
  request(item) {
    const name = field("name").value.trim();
    if (!name) { submitter.say(t("fin_need_name")); return null; }
    const values = { name, name_en: field("name_en").value.trim() };
    return item
      ? { method: "PATCH", url: `/api/v1/expense-categories/${item.id}`, body: values, label: t("materials_saved", { name }) }
      : { method: "POST", url: "/api/v1/expense-categories", body: values, label: t("materials_saved", { name }) };
  },
  done(data) { toast(t("materials_saved", { name: data.category.name })); reloadTab(); },
}));

const categoryRemoveDialog = setup("category-remove-dialog", () => ({
  request(item) {
    return { method: "PATCH", url: `/api/v1/expense-categories/${item.id}`, body: { is_active: false }, label: t("removed", { name: item.name }) };
  },
  done(_data, item) { toast(t("removed", { name: item.name })); reloadTab(); },
}));

async function restoreCategory(item, button) {
  if (button.disabled) return;
  button.disabled = true;
  const result = await save("PATCH", `/api/v1/expense-categories/${item.id}`, { is_active: true }, { key: newKey(), label: t("restored", { name: item.name }) });
  button.disabled = false;
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { alertMessage(t("unknown")); return; }
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
  toast(t("restored", { name: item.name }));
  reloadTab();
}

// --- staff pay ---

const statementDialog = document.getElementById("statement-dialog");
let statementMember = null;
if (statementDialog) wireClose(statementDialog);

async function loadStatement(member) {
  const holder = statementDialog.querySelector("[data-statement-holder]");
  const result = await fetchText(`/finances/pay/${member.id}?month=${encodeURIComponent(month)}`);
  if (result.status !== 200) { holder.textContent = errorMessage(result, t("failed")); return; }
  holder.replaceChildren(parseFragment(result.text));
}

async function openStatement(member, opener) {
  statementMember = member;
  const holder = statementDialog.querySelector("[data-statement-holder]");
  holder.textContent = t("loading");
  openDialog(statementDialog, opener);
  await loadStatement(member);
}

const percentBp = (text) => {
  const t2 = asciiDigits(text).trim().replace("٫", ".");
  if (t2 === "") return 0;
  const m = /^(\d{1,3})(?:\.(\d{1,2}))?$/.exec(t2);
  if (!m) return null;
  const bp = Number(m[1]) * 100 + Number((m[2] || "").padEnd(2, "0") || "0");
  return bp <= 10000 ? bp : null;
};
const percentText = (bp) => (bp % 100 ? (bp / 100).toFixed(2).replace(/0$/, "") : String(bp / 100));

const ruleDialog = setup("rule-dialog", ({ field, submitter }) => ({
  fill(item) {
    field("percent").value = item.percent_bp ? percentText(item.percent_bp) : "";
    for (const name of ["monthly_salary", "per_visit", "per_procedure"]) field(name).value = item[name] ? amountInput(item[name]) : "";
  },
  request(item) {
    const percent = percentBp(field("percent").value);
    if (percent === null) { submitter.say(t("fin_bad_percent")); return null; }
    const values = { percent_bp: percent, version: item.version };
    for (const name of ["monthly_salary", "per_visit", "per_procedure"]) {
      const raw = field(name).value.trim();
      const p = raw === "" ? 0 : parsePiastres(raw);
      if (p === null) { submitter.say(t("bad_amount")); return null; }
      values[name] = p;
    }
    return { method: "PUT", url: `/api/v1/finances/pay/${item.id}`, body: values, label: t("fin_rule_saved", { name: item.name }) };
  },
  done(_data, item) { toast(t("fin_rule_saved", { name: item.name })); reloadTab(); },
}));

let paidAmount = 0;
const payoutDialog = setup("payout-dialog", ({ dialog, field, submitter }) => ({
  fill(item) {
    dialog.querySelector("[data-balance-text]").textContent = moneyText(item.balance);
    field("amount").value = item.balance > 0 ? amountInput(item.balance) : "";
  },
  request(item) {
    const amount = money(submitter, field("amount").value);
    if (amount === null) return null;
    const method = dialog.querySelector('[name="method"]:checked').value;
    paidAmount = amount;
    return { method: "POST", url: `/api/v1/finances/pay/${item.id}/payouts`,
             body: { month, amount, method, note: field("note").value.trim() },
             label: t("fin_paid", { name: item.name, amount: moneyText(amount) }) };
  },
  async done(_data, item) {
    toast(t("fin_paid", { name: item.name, amount: moneyText(paidAmount) }));
    if (statementDialog && statementDialog.open) await loadStatement(item);
    reloadTab();
  },
}));

// --- "Load more" for shifts, expenses and patients who owe ---

const MORE = { shifts: "/finances/shifts?", expenses: `/finances/expenses?month=${encodeURIComponent(month)}&`, owed: "/finances/owed?" };

async function loadMore(button) {
  if (button.disabled) return;
  const kind = button.dataset.more;
  const list = body.querySelector(`[data-rows="${kind}"]`);
  button.disabled = true;
  const result = await fetchText(`${MORE[kind]}after=${encodeURIComponent(button.dataset.next)}`);
  button.disabled = false;
  if (result.status !== 200) { alertMessage(errorMessage(result, t("failed"))); return; }
  const rows = parseFragment(result.text);
  // A day already headed on the page isn't headed twice.
  const first = rows.querySelector(".day-head");
  const heads = list.querySelectorAll(".day-head");
  if (first && heads.length && heads[heads.length - 1].dataset.day === first.dataset.day) first.remove();
  list.append(rows);
  const next = result.headers.get("X-Next");
  if (next) button.dataset.next = next; else button.closest(".list-foot").remove();
}

// --- one listener for every button ---

document.addEventListener("click", (event) => {
  const target = event.target;
  const on = (selector) => target.closest(selector);
  let button;
  if ((button = on("[data-cash]")) && cashDialog) cashDialog.open({ kind: button.dataset.cash }, button);
  else if ((button = on("[data-open-shift]")) && openShiftDialog) openShiftDialog.open(null, button);
  else if ((button = on("[data-close-shift]")) && closeShiftDialog) {
    const shiftId = button.closest("[data-shift-id]").dataset.shiftId;
    closeShiftDialog.open({ id: Number(shiftId), expected: Number(button.dataset.expectedAmount) }, button);
  } else if ((button = on("[data-shift-report]")) && reportDialog) openReport(button.dataset.shiftReport, button);
  else if ((button = on("[data-new-expense]")) && expenseDialog) expenseDialog.open({ fromDrawer: button.hasAttribute("data-from-drawer") }, button);
  else if ((button = on("[data-void-expense]")) && voidDialog) voidDialog.open(dataOf(button, "expense"), button);
  else if ((button = on("[data-new-category]")) && categoryDialog) categoryDialog.open(null, button);
  else if ((button = on("[data-rename-category]")) && categoryDialog) categoryDialog.open(dataOf(button, "category"), button);
  else if ((button = on("[data-remove-category]")) && categoryRemoveDialog) categoryRemoveDialog.open(dataOf(button, "category"), button);
  else if ((button = on("[data-restore-category]"))) restoreCategory(dataOf(button, "category"), button);
  else if ((button = on("[data-statement]")) && statementDialog) openStatement(dataOf(button, "member"), button);
  else if ((button = on("[data-edit-rule]")) && ruleDialog) ruleDialog.open(dataOf(button, "member"), button);
  else if ((button = on("[data-new-payout]")) && payoutDialog && statementMember) {
    const holder = statementDialog.querySelector("[data-statement-body]");
    const balance = holder ? Number(holder.dataset.balance) : 0;
    payoutDialog.open({ ...statementMember, balance }, button);
  } else if ((button = on("[data-more]"))) loadMore(button);
});

// "Close the drawer" on Today lands here with ?close=1 (Saged, 2026-11-22): the count opens at once.
if (new URLSearchParams(window.location.search).get("close") === "1") {
  const closer = document.querySelector("[data-close-shift]");
  if (closer) closer.click();
  history.replaceState(null, "", window.location.pathname);
}
