// Team & settings: each procedure's follow-up recipe (Saged, 2026-11-13). The sessions the doctor is offered after
// recording the procedure: edit them, turn them off, or add them to another procedure. One PATCH per save
// (procedures/{id}, changes.follow_up), with the dialog's request key until a definite answer.
import { errorMessage, get, isOk } from "./api.js";
import { Submitter, el, openDialog, wireClose } from "./dialogs.js";
import { asciiDigits, localDigits } from "./numbers.js";
import { toast } from "./shell.js";
import { t } from "./strings.js";

const card = document.querySelector("[data-recipes]");
const dialog = document.getElementById("recipe-dialog");
if (card && dialog) {
  const list = card.querySelector("[data-recipe-list]");
  const status = card.querySelector("[data-recipe-status]");
  const search = card.querySelector("[data-recipe-search]");
  const steps = dialog.querySelector("[data-recipe-steps]");
  const form = dialog.querySelector("form");
  const submitter = new Submitter(dialog, { label: t("recipe_title") });
  const english = document.documentElement.lang === "en";
  const pick = dialog.querySelector("[data-recipe-pick]");
  const pickSelect = pick.querySelector("select");
  // A doctor keeps the follow-ups of their own sections only (Saged, 2026-11-22); the owner every section's.
  const mine = card.dataset.sections ? new Set(card.dataset.sections.split(",").map(Number)) : null;
  let procedures = [];
  let editing = null;
  let stepNo = 0;  // each step's radio group needs its own name
  const PRICES = [["none", "vp_none"], ["follow_up", "vp_follow_up"], ["consultation", "vp_consultation"]];
  wireClose(dialog);

  const labelOf = (step) => (english && step.label_en ? step.label_en : step.label);
  const summary = (p) => p.follow_up.map((s) => `${t("in_days", { n: localDigits(s.days) })}: ${labelOf(s)}`).join(" · ");

  function render() {
    const q = search.value.trim().toLocaleLowerCase();
    const shown = (q ? procedures.filter((p) => p.name.toLocaleLowerCase().includes(q)) : procedures.filter((p) => p.follow_up.length)).slice(0, 40);
    list.replaceChildren(...shown.map((p) => {
      const li = el("li");
      const what = el("span");
      const name = el("bdi", null, p.name);
      name.dir = "auto";
      what.append(el("strong", null, ""), el("br"), el("span", "muted", p.follow_up.length ? summary(p) : t("recipe_none")));
      what.firstChild.append(name);
      const edit = el("button", "btn ghost", p.follow_up.length ? t("recipe_edit") : t("recipe_add"));
      edit.type = "button";
      edit.setAttribute("aria-label", `${edit.textContent}: ${p.name}`);
      edit.addEventListener("click", () => open(p, edit));
      li.append(what, edit);
      return li;
    }));
    status.textContent = shown.length ? "" : q ? t("proc_none") : t("recipe_none");
  }

  // A step's visit fee (chips) and the procedure done in that session (one of this procedure's section, or none).
  function moreFields(step) {
    const more = el("div", "recipe-more");
    const prices = el("fieldset", "group");
    prices.append(el("legend", null, t("recipe_visit_price")));
    const chips = el("div", "chips");
    stepNo += 1;
    for (const [value, key] of PRICES) {
      const chip = el("label", "chip");
      const input = el("input");
      input.type = "radio";
      input.name = `visit_price_${stepNo}`;
      input.value = value;
      input.checked = (step.visit_price || "none") === value;
      chip.append(input, el("span", null, t(key)));
      chips.append(chip);
    }
    prices.append(chips);
    const select = el("select");
    select.name = "procedure_id";
    select.append(Object.assign(el("option", null, t("recipe_no_proc")), { value: "" }));
    for (const p of procedures.filter((x) => x.section_id === editing.section_id)) {
      select.append(Object.assign(el("option", null, p.name), { value: String(p.id) }));
    }
    select.value = step.procedure_id ? String(step.procedure_id) : "";
    if (step.procedure_id && select.value !== String(step.procedure_id)) select.value = "";  // no longer offered
    const procLabel = el("label", "field", t("recipe_proc"));
    procLabel.append(select);
    more.append(prices, procLabel);
    return more;
  }

  function addStep(step = { days: 7, label: "", label_en: "", visit_price: "none", procedure_id: null }) {
    const li = el("li", "recipe-step");
    li.step = step;
    const name = el("input");
    name.name = "label";
    name.value = labelOf(step);
    name.maxLength = 80;
    name.dir = "auto";
    name.autocomplete = "off";
    const nameLabel = el("label", "field", t("recipe_session"));
    nameLabel.append(name);
    const days = el("input");
    days.name = "days";
    days.value = localDigits(step.days);
    days.inputMode = "numeric";
    days.maxLength = 3;
    days.dir = "ltr";
    days.autocomplete = "off";
    const daysLabel = el("label", "field", t("recipe_days"));
    daysLabel.append(days);
    const remove = el("button", "icon-btn", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", t("remove"));
    remove.addEventListener("click", () => li.remove());
    li.append(nameLabel, daysLabel, remove, moreFields(step));
    steps.append(li);
    return name;
  }

  // "Add follow-ups" (Saged, 2026-11-22): pick a procedure that has none yet, then its sessions.
  function openNew(opener) {
    if (submitter.holdsUnknown()) { openDialog(dialog, opener); return; }
    const free = procedures.filter((p) => !p.follow_up.length);
    if (!free.length) { toast(t("recipe_all_set")); return; }
    pickSelect.replaceChildren(Object.assign(el("option", null, t("recipe_pick")), { value: "" }),
      ...free.map((p) => Object.assign(el("option", null, p.name), { value: String(p.id) })));
    editing = null;
    submitter.reset();
    steps.replaceChildren();
    dialog.querySelector("[data-recipe-name]").textContent = "";
    pick.hidden = false;
    openDialog(dialog, opener);
    pickSelect.focus();
  }

  pickSelect.addEventListener("change", () => {
    const p = procedures.find((x) => String(x.id) === pickSelect.value);
    editing = p || null;
    steps.replaceChildren();
    dialog.querySelector("[data-recipe-name]").textContent = p ? p.name : "";
    if (p) addStep();
  });

  function open(p, opener) {
    if (!submitter.holdsUnknown()) {
      pick.hidden = true;
      editing = p;
      submitter.reset();
      steps.replaceChildren();
      p.follow_up.forEach((s) => addStep(s));
      if (!p.follow_up.length) addStep();
      dialog.querySelector("[data-recipe-name]").textContent = p.name;
    }
    openDialog(dialog, opener);
  }

  function read() {
    const out = [];
    for (const li of steps.children) {
      const text = li.querySelector('[name="label"]').value.trim();
      const days = Number(asciiDigits(li.querySelector('[name="days"]').value.trim()));
      if (!text) return null;
      if (!Number.isInteger(days) || days < 1 || days > 730) return null;
      const old = li.step;
      const extra = {
        visit_price: (li.querySelector('input[type="radio"]:checked') || { value: "none" }).value,
        procedure_id: Number(li.querySelector('[name="procedure_id"]').value) || null,
      };
      out.push(english ? { days, label: old.label || text, label_en: text, ...extra }
        : { days, label: text, label_en: old.label === text ? old.label_en || "" : "", ...extra });
    }
    return out;
  }

  async function saveRecipe(recipe) {
    const body = submitter.unknown ? submitter.unknown.body : { changes: { follow_up: recipe } };
    const result = await submitter.run("PATCH", `/api/v1/procedures/${editing.id}`, body, `${t("recipe_title")} · ${editing.name}`);
    if (!result || !isOk(result)) return;
    editing.follow_up = result.data.procedure.follow_up || [];
    dialog.close();
    render();
    toast(t("recipe_saved", { name: editing.name }));
  }

  card.querySelector("[data-recipe-new]").addEventListener("click", (event) => openNew(event.currentTarget));
  dialog.querySelector("[data-recipe-add]").addEventListener("click", () => {
    if (!editing) { submitter.say(t("recipe_pick")); pickSelect.focus(); return; }
    if (steps.children.length >= 6) { submitter.say(t("recipe_max")); return; }
    addStep().focus();
  });
  dialog.querySelector("[data-recipe-off]").addEventListener("click", () => { if (!submitter.unknown && editing) saveRecipe([]); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitter.unknown) { saveRecipe(null); return; }
    if (!editing) { submitter.say(t("recipe_pick")); pickSelect.focus(); return; }
    const recipe = read();
    if (!recipe) { submitter.say(t("recipe_bad")); return; }
    saveRecipe(recipe);
  });
  search.addEventListener("input", render);

  (async () => {
    status.textContent = t("loading");
    const result = await get("/api/v1/procedures");
    if (!isOk(result)) { status.textContent = errorMessage(result, t("failed")); return; }
    procedures = result.data.items.filter((p) => !mine || mine.has(p.section_id)).map((p) => ({ id: p.id, name: p.name, section_id: p.section_id, follow_up: p.follow_up || [] }));
    render();
  })();
}
