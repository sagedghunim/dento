// The dental chart (design E3): tap a tooth to see its history, record a finding, a procedure or a plan item.
// Each form in the panel owns one request key until a definite answer (dialogs.Submitter). A finding sends the
// condition the screen showed (expected_condition), so a stale screen gets 409 with the tooth as it is now.
import { errorMessage, fetchText, isOk } from "./api.js";
import { Submitter, parseFragment } from "./dialogs.js";
import { localDigits, moneyText } from "./numbers.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const data = JSON.parse(document.getElementById("chart-data").textContent);
const labels = Object.fromEntries(data.conditions);
const takesSurfaces = (condition) => data.surface_conditions.includes(condition);
const teeth = {};       // number -> {condition, surfaces, recorded, in_progress}
for (const [n, state] of Object.entries(data.teeth)) teeth[Number(n)] = state;
const done = new Set(data.done);
let planItems = data.plan_items;  // open items: {id, tooth, procedure_id, status, price, name, plan_id?}
let planId = data.plan_id;
const procedureNames = Object.fromEntries(data.procedures.map((p) => [p.id, p.name]));

const panel = document.querySelector("[data-panel]");
const layout = document.querySelector("[data-chart-layout]");
let current = null;     // the open tooth number
let opener = null;

const state = (n) => teeth[n] || { condition: "healthy", surfaces: [], note: "", recorded: false };
// "In progress" means the tooth is in an open plan item (design E3); the page holds every open item.
const inProgress = (n) => planItems.some((i) => i.tooth === n);

function describe(n) {
  const s = state(n);
  let text = `${t("tooth_n", { n: localDigits(n) })}: ${labels[s.condition] || s.condition}`;
  if (s.surfaces.length) text += ` (${s.surfaces.join(" ")})`;
  if (inProgress(n)) text += `. ${t("in_progress")}`;
  if (done.has(n)) text += `. ${t("has_procedures")}`;
  return text;
}

// --- the chart ---

function paint(n) {
  const s = state(n);
  document.querySelectorAll(`.tooth[data-tooth="${n}"]`).forEach((button) => {
    button.dataset.condition = s.condition;
    button.toggleAttribute("data-progress", inProgress(n));
    button.toggleAttribute("data-done", done.has(n));
    button.querySelector("[data-label]").textContent = describe(n);
    button.title = describe(n);
  });
}
document.querySelectorAll(".tooth").forEach((b) => { b.title = describe(Number(b.dataset.tooth)); });

document.querySelectorAll('[name="dentition"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    document.querySelectorAll("[data-dentition]").forEach((set) => { set.hidden = set.dataset.dentition !== radio.value; });
  });
});

document.addEventListener("click", (event) => {
  const tooth = event.target.closest(".tooth");
  if (tooth) open(Number(tooth.dataset.tooth), tooth);
});

// --- the panel ---

const busy = () => Boolean(panel.querySelector("[data-part][data-busy], [data-part][data-unknown]"));

function open(n, button) {
  if (current === n && !panel.hidden) { panel.querySelector("#panel-title").focus(); return; }
  if (busy()) { alertMessage(t("finish_first")); return; }
  current = n;
  opener = button;
  document.querySelectorAll(".tooth[aria-pressed='true']").forEach((b) => b.setAttribute("aria-pressed", "false"));
  document.querySelectorAll(`.tooth[data-tooth="${n}"]`).forEach((b) => b.setAttribute("aria-pressed", "true"));
  panel.querySelector("[data-panel-no]").textContent = localDigits(n);
  showState();
  parts.forEach((part) => part.reset());
  panel.hidden = false;
  layout.classList.add("with-panel");
  panel.querySelector("#panel-title").focus();
  loadHistory(false);
}

function close() {
  if (busy()) { alertMessage(t("finish_first")); return; }
  panel.hidden = true;
  layout.classList.remove("with-panel");
  document.querySelectorAll(".tooth[aria-pressed='true']").forEach((b) => b.setAttribute("aria-pressed", "false"));
  current = null;
  if (opener && opener.isConnected) opener.focus();
}
panel.querySelector("[data-panel-close]").addEventListener("click", close);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !panel.hidden && !document.querySelector("details.menu[open]")) close();
});

function showState() {
  const s = state(current);
  panel.querySelector("[data-panel-key]").dataset.condition = s.condition;
  let text = labels[s.condition] || s.condition;
  if (s.surfaces.length) text += ` (${s.surfaces.join(" ")})`;
  if (inProgress(current)) text += ` · ${t("in_progress")}`;
  panel.querySelector("[data-panel-state]").textContent = text;
}

function changed(n, tooth) {
  if (tooth) teeth[n] = { condition: tooth.condition, surfaces: tooth.surfaces || [], note: tooth.note || "", recorded: true };
  paint(n);
  if (n === current) { showState(); loadHistory(false); }
}

// A procedure removed on the visit screen put its tooth back (Saged, 2026-11-17): no row now is a healthy tooth.
window.addEventListener("hamada:procedure-removed", (event) => {
  const n = event.detail.tooth_number;
  if (!n) return;
  if (!event.detail.tooth) delete teeth[n];
  changed(n, event.detail.tooth);
});

// Surfaces show for caries and fillings only (design E3).
function surfacesFor(form, condition) {
  const box = form.querySelector("[data-surfaces]");
  if (!box) return;
  box.hidden = !takesSurfaces(condition);
}
const checkedSurfaces = (form) => Array.from(form.querySelectorAll('[name="surface"]:checked'), (c) => c.value);

// --- findings: PUT patients/{id}/teeth/{n} ---

function findingPart(section) {
  const form = section.querySelector("form");
  const submitter = new Submitter(section, { label: t("finding") });
  form.addEventListener("change", (event) => {
    if (event.target.name === "condition") surfacesFor(form, event.target.value);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const n = current;
    let body = submitter.unknown ? submitter.unknown.body : null;
    if (!body) {
      const picked = form.querySelector('[name="condition"]:checked');
      const condition = picked ? picked.value : "healthy";
      const s = state(n);
      body = { condition, surfaces: takesSurfaces(condition) ? checkedSurfaces(form) : [],
               note: form.elements.namedItem("note").value.trim(), expected_condition: s.recorded ? s.condition : null };
      if (data.appointment_id) body.appointment_id = data.appointment_id;
    }
    const url = submitter.unknown ? submitter.unknown.url : `/api/v1/patients/${data.patient_id}/teeth/${n}`;
    const result = await submitter.run("PUT", url, body, `${t("finding")} · ${t("tooth_n", { n: localDigits(n) })}`);
    if (!result) return;
    const error = result.data && result.data.error;
    if (result.status === 409 && error && error.code === "stale") {
      // Someone changed it meanwhile: show the tooth as it is now; the person decides again.
      teeth[n] = error.tooth ? { condition: error.tooth.condition, surfaces: error.tooth.surfaces || [], note: error.tooth.note || "", recorded: true }
        : { condition: "healthy", surfaces: [], note: "", recorded: false };
      changed(n, null);
      fill();
      return;
    }
    if (!isOk(result)) return;
    changed(n, result.data.tooth);
    toast(t("finding_saved", { n: localDigits(n), condition: labels[result.data.tooth.condition] }));
  });
  function fill() {
    const s = state(current);
    form.reset();
    const radio = form.querySelector(`[name="condition"][value="${s.condition}"]`);
    if (radio) radio.checked = true;
    s.surfaces.forEach((v) => { const c = form.querySelector(`[name="surface"][value="${v}"]`); if (c) c.checked = true; });
    form.elements.namedItem("note").value = s.note || "";
    surfacesFor(form, s.condition);
  }
  return { reset() { if (!submitter.holdsUnknown()) { submitter.reset(); fill(); } } };
}

// --- a procedure in this visit: POST appointments/{id}/procedures (an invoice line) ---

function procedurePart(section) {
  const form = section.querySelector("form");
  const select = form.elements.namedItem("procedure_id");
  const picks = section.querySelector("[data-plan-picks]");
  const submitter = new Submitter(section, { label: t("record") });
  if (select) select.addEventListener("change", () => {
    const option = select.selectedOptions[0];
    surfacesFor(form, option ? option.dataset.condition : "");
  });

  async function send(body, name) {
    const n = current;
    const label = `${t("record")} · ${name} · ${t("tooth_n", { n: localDigits(n) })}`;
    const result = await submitter.run("POST", `/api/v1/appointments/${data.appointment_id}/procedures`, body, label);
    if (!result || !isOk(result)) return;
    const record = result.data.procedure;
    window.dispatchEvent(new CustomEvent("hamada:procedure-recorded", { detail: result.data }));  // the visit screen's list
    done.add(record.tooth);
    if (record.plan_item_id) planItems = planItems.filter((i) => i.id !== record.plan_item_id);
    changed(record.tooth, result.data.tooth);
    toast(t("recorded", { n: localDigits(record.tooth), name }));
    fillPicks();
    clear();
  }

  function clear() { form.reset(); surfacesFor(form, ""); }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitter.unknown) { send(submitter.unknown.body, submitter.lastLabel || ""); return; }
    const id = Number(select && select.value);
    if (!id) { submitter.say(t("need_procedure")); return; }
    const option = select.selectedOptions[0];
    const body = { procedure_id: id, tooth: current, note: form.elements.namedItem("note").value.trim() };
    if (takesSurfaces(option.dataset.condition)) body.surfaces = checkedSurfaces(form);
    send(body, procedureNames[id]);
  });

  // The tooth's open plan items: one tap records the planned procedure at the plan's price.
  function fillPicks() {
    const mine = planItems.filter((i) => i.tooth === current && procedureNames[i.procedure_id]);
    picks.replaceChildren(...mine.map((item) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn ghost wide plan-pick";
      b.dataset.run = "";
      // No price for someone without money.view: the server sends none (Saged, 2026-11-16).
      b.textContent = item.price == null ? item.name : t("from_plan", { name: item.name, amount: moneyText(item.price) });
      b.addEventListener("click", () => {
        if (submitter.unknown) return;
        send({ procedure_id: item.procedure_id, tooth: current, plan_item_id: item.id }, item.name);
      });
      return b;
    }));
  }
  return {
    reset() { if (!submitter.holdsUnknown()) { submitter.reset(); clear(); fillPicks(); } },
    planChanged() { if (!submitter.holdsUnknown() && !section.dataset.busy) fillPicks(); },
  };
}

// --- add to the treatment plan: the section's open plan, or a new one ---

function planPart(section) {
  const form = section.querySelector("form");
  const select = form.elements.namedItem("procedure_id");
  const submitter = new Submitter(section, { label: t("add_to_plan") });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const n = current;
    let request = submitter.unknown;
    if (!request) {
      const id = Number(select.value);
      if (!id) { submitter.say(t("need_procedure")); return; }
      const items = [{ procedure_id: id, tooth: n }];
      request = planId ? { url: `/api/v1/plans/${planId}/items`, body: { items } }
        : { url: `/api/v1/patients/${data.patient_id}/plans`, body: { section_id: data.section_id, items } };
    }
    const name = procedureNames[request.body.items[0].procedure_id] || "";
    const result = await submitter.run("POST", request.url, request.body,
      `${t("add_to_plan")} · ${name} · ${t("tooth_n", { n: localDigits(n) })}`);
    if (!result || !isOk(result)) return;
    const plan = result.data.plan;
    planId = plan.id;
    planItems = planItems.filter((i) => i.plan_id !== plan.id && !plan.items.some((p) => p.id === i.id))
      .concat(plan.items.filter((i) => i.status === "planned" || i.status === "in_progress").map((i) => ({
        id: i.id, plan_id: plan.id, tooth: i.tooth, procedure_id: i.procedure_id, status: i.status, price: i.price,
        name: procedureNames[i.procedure_id] || i.name_ar })));
    form.reset();
    changed(request.body.items[0].tooth, null);
    parts.forEach((part) => { if (part.planChanged) part.planChanged(); });
    toast(t("planned_added", { name, n: localDigits(request.body.items[0].tooth) }));
  });
  return { reset() { if (!submitter.holdsUnknown()) { submitter.reset(); form.reset(); } } };
}

const builders = { finding: findingPart, procedure: procedurePart, plan: planPart };
const parts = Array.from(panel.querySelectorAll("[data-part]"), (section) => builders[section.dataset.part](section));

// --- the tooth's history, newest first, formatted on the server ---

const historyList = panel.querySelector("[data-history]");
const historyStatus = panel.querySelector("[data-history-status]");
const historyMore = panel.querySelector("[data-history-more]");
let historyController = null;

async function loadHistory(more) {
  const n = current;
  if (historyController) historyController.abort();
  historyController = new AbortController();
  let url = `/patients/${data.patient_id}/teeth/${n}/history`;
  if (more) url += `?after=${encodeURIComponent(historyMore.dataset.next)}`;
  else { historyList.replaceChildren(); historyMore.hidden = true; }
  historyStatus.textContent = t("loading");
  const result = await fetchText(url, { signal: historyController.signal });
  if (result.aborted || n !== current) return;
  if (result.status !== 200) { historyStatus.textContent = errorMessage(result, t("history_failed")); return; }
  historyList.append(parseFragment(result.text));
  const next = result.headers.get("X-Next") || "";
  historyMore.hidden = !next;
  historyMore.dataset.next = next;
  historyStatus.textContent = historyList.children.length ? "" : t("no_history");
}
historyMore.addEventListener("click", () => loadHistory(true));

// --- the visits by date above the chart (Saged, 2026-11-19): one open at a time; its teeth light up ---
const dated = document.querySelector("[data-dated]");
if (dated) {
  dated.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-date-visit]");
    if (!chip) return;
    const opening = chip.getAttribute("aria-pressed") !== "true";
    dated.querySelectorAll("[data-date-visit]").forEach((c) => c.setAttribute("aria-pressed", "false"));
    dated.querySelectorAll("[data-date-detail]").forEach((d) => { d.hidden = true; });
    document.querySelectorAll(".tooth.lit").forEach((t) => t.classList.remove("lit"));
    if (!opening) return;
    chip.setAttribute("aria-pressed", "true");
    dated.querySelector(`[data-date-detail="${chip.dataset.dateVisit}"]`).hidden = false;
    for (const n of (chip.dataset.teeth || "").split(",").filter(Boolean)) {
      document.querySelectorAll(`.tooth[data-tooth="${n}"]`).forEach((t) => t.classList.add("lit"));
    }
  });
}
