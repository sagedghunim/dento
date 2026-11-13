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
  let procedures = [];
  let editing = null;
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

  function addStep(step = { days: 7, label: "", label_en: "" }) {
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
    li.append(nameLabel, daysLabel, remove);
    steps.append(li);
    return name;
  }

  function open(p, opener) {
    if (!submitter.holdsUnknown()) {
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
      out.push(english ? { days, label: old.label || text, label_en: text }
        : { days, label: text, label_en: old.label === text ? old.label_en || "" : "" });
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

  dialog.querySelector("[data-recipe-add]").addEventListener("click", () => {
    if (steps.children.length >= 6) { submitter.say(t("recipe_max")); return; }
    addStep().focus();
  });
  dialog.querySelector("[data-recipe-off]").addEventListener("click", () => { if (!submitter.unknown) saveRecipe([]); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitter.unknown) { saveRecipe(null); return; }
    const recipe = read();
    if (!recipe) { submitter.say(t("recipe_bad")); return; }
    saveRecipe(recipe);
  });
  search.addEventListener("input", render);

  (async () => {
    status.textContent = t("loading");
    const result = await get("/api/v1/procedures");
    if (!isOk(result)) { status.textContent = errorMessage(result, t("failed")); return; }
    procedures = result.data.items.map((p) => ({ id: p.id, name: p.name, follow_up: p.follow_up || [] }));
    render();
  })();
}
