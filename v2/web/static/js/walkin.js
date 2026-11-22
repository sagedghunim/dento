// "Walk-in now" (design G5): a patient who is here now, in one request. The doctor and the service open on what
// the server would choose (GET /today/walk-in) and can be changed; a payment is optional. POST
// /appointments/walk-in books today at the next turn, marked arrived, under one key until a definite answer.
import { errorMessage, fetchText, get, isOk } from "./api.js";
import { hideConflict, patientFrom, patientMeta, showConflict } from "./book.js";
import { courseOf, courseService, loadCourses, Submitter, el, openDialog, parseFragment, wireClose } from "./dialogs.js";
import { amountInput, asciiDigits, localDigits, moneyText, parsePiastres } from "./numbers.js";
import { initNewPatient } from "./newpatient.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

export function initWalkIn({ onBooked } = {}) {
  const dialog = document.getElementById("walkin-dialog");
  if (!dialog) return null;
  const form = dialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const submitter = new Submitter(dialog, { label: t("walkin") });
  const finder = dialog.querySelector("[data-finder]");
  const results = dialog.querySelector("[data-results]");
  const searchStatus = dialog.querySelector("[data-search-status]");
  const picked = dialog.querySelector("[data-picked]");
  const newBox = dialog.querySelector("[data-new]");
  const note = dialog.querySelector("[data-defaults-note]");
  const fee = dialog.querySelector("[data-fee]");
  const collectFields = dialog.querySelector("[data-collect-fields]");
  let patient = null;
  let searchTimer = null;
  let searchController = null;
  let defaults = { doctor_id: null, service_id: null };
  let quoteSeq = 0;
  let openSeq = 0;
  let lastFee = null;
  let confirmOther = false;  // "Book anyway" on another doctor's booking today
  const np = initNewPatient(dialog);
  wireClose(dialog);

  // --- the patient: search, or a new name and phone ---

  function showPicked(p) {
    patient = p;
    confirmOther = false;
    hideConflict(dialog);
    picked.hidden = false;
    finder.hidden = true;
    if (newBox) newBox.hidden = true;
    const name = dialog.querySelector("[data-picked-name]");
    const who = el("bdi", null, p.name);
    who.dir = "auto";
    name.replaceChildren(who);
    dialog.querySelector("[data-picked-meta]").replaceChildren(...patientMeta(p));
    loadCourses(dialog, p.id).then(() => { if (patient === p && courseOf(dialog)) followCourse(); });
    quote();
  }

  function showFinder() {
    loadCourses(dialog, null);
    patient = null;
    picked.hidden = true;
    finder.hidden = false;
    if (newBox) newBox.hidden = true;
    field("q").focus();
    quote();
  }

  function showNew() {
    patient = null;
    picked.hidden = true;
    finder.hidden = true;
    newBox.hidden = false;
    np.prefill(field("q").value.trim());
    quote();
  }

  async function search() {
    const q = field("q").value.trim();
    if (searchController) searchController.abort();
    if (q.length < 2) { results.replaceChildren(); searchStatus.textContent = ""; return; }
    searchController = new AbortController();
    searchStatus.textContent = t("searching");
    const result = await fetchText(`/patients/search?pick=1&q=${encodeURIComponent(q)}`, { signal: searchController.signal });
    if (result.aborted) return;
    if (result.status !== 200) { searchStatus.textContent = errorMessage(result, t("search_failed")); return; }
    results.replaceChildren(parseFragment(result.text));
    const count = results.querySelectorAll("[data-pick-patient]").length;
    searchStatus.textContent = count ? t("results", { n: count }) : t("no_results");
  }

  field("q").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 250); });
  field("q").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); clearTimeout(searchTimer); search(); }
  });
  dialog.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick-patient]");
    if (pick) { event.preventDefault(); showPicked(patientFrom(pick)); }
    if (event.target.closest("[data-change-patient]")) showFinder();
    if (event.target.closest("[data-new-patient]")) showNew();
    if (event.target.closest("[data-back-to-search]")) showFinder();
  });

  // --- doctor and service: the server's choice first, both changeable ---

  const doctorId = () => Number(field("doctor").value) || defaults.doctor_id || null;
  const serviceId = () => Number(field("service").value) || null;

  // How many of a procedure (Saged, 2026-11-19): shown for a procedure service only.
  const qtyBox = form.querySelector("[data-qty-box]");
  const isProcedure = () => { const o = field("service").selectedOptions[0]; return Boolean(o && o.dataset.kind === "procedure"); };
  const qty = () => {
    if (!isProcedure()) return 1;
    const n = Number(asciiDigits(field("qty").value.trim()));
    return Number.isInteger(n) && n >= 1 && n <= 20 ? n : 1;
  };

  // Only the services of the chosen doctor's sections (a doctor's sections are those of their sessions today).
  function filterServices() {
    const option = field("doctor").selectedOptions[0];
    const sections = option && option.dataset.sections ? option.dataset.sections.split(",") : [];
    for (const o of field("service").options) {
      if (!o.value) continue;
      o.hidden = sections.length > 0 && Boolean(o.dataset.section) && !sections.includes(o.dataset.section);
      o.disabled = o.hidden;  // Safari shows hidden options
    }
    pickService();
  }

  // No "usual consultation" choice (Saged, 2026-11-22): a real service is always picked, the clinic's default for this
  // doctor when it is offered, else the first one offered.
  function pickService(preferred) {
    const select = field("service");
    const usable = [...select.options].filter((o) => o.value && !o.disabled);
    const want = preferred && usable.find((o) => o.value === String(preferred));
    const current = usable.find((o) => o.value === select.value);
    const choice = want || current || usable.find((o) => o.value === String(defaults.service_id || "")) || usable[0];
    select.value = choice ? choice.value : "";
  }

  // A visit that goes on with a treatment takes that treatment's service (Saged, 2026-11-22).
  function followCourse() {
    const id = courseService(dialog);
    pickService(id || defaults.service_id);
    if (id && serviceId() !== id) pickService(defaults.service_id);
    quote();
  }

  form.addEventListener("change", (event) => {
    const name = event.target.name;
    if (name === "doctor") { field("service").value = ""; filterServices(); quote(); }  // filterServices picks one
    if (name === "service") quote();
    if (name === "qty") quote();
    if (name === "continues_id") followCourse();
    if (name === "amount") field("amount").dataset.touched = "1";
    if (name === "collect") { collectFields.hidden = !field("collect").checked; if (field("collect").checked) field("amount").focus(); }
  });

  async function loadDefaults(seq) {
    note.textContent = t("loading");
    note.hidden = false;
    const result = await get("/today/walk-in");
    if (seq !== openSeq) return;
    if (!isOk(result)) { note.textContent = errorMessage(result, ""); note.hidden = !note.textContent; return; }
    defaults = result.data;
    note.textContent = defaults.message || "";
    note.hidden = !note.textContent;
    if (!field("doctor").value && defaults.doctor_id) {
      field("doctor").value = String(defaults.doctor_id);
      if (field("doctor").value !== String(defaults.doctor_id)) field("doctor").value = "";  // not in the list
      field("service").value = "";
      filterServices();
    }
    pickService(field("doctor").value && Number(field("doctor").value) === defaults.doctor_id ? defaults.service_id : null);
    quote();
  }

  // The fee for this patient (a follow-up may be free); it fills the amount until the person types one.
  async function quote() {
    const doctor = doctorId();
    const service = serviceId();
    const seq = ++quoteSeq;
    qtyBox.hidden = !isProcedure();
    if (!doctor || !service) { fee.textContent = ""; lastFee = null; return; }
    let url = `/api/v1/appointments/quote?doctor=${doctor}&service=${service}&day=${encodeURIComponent(config().today)}`;
    if (patient) url += `&patient=${patient.id}`;
    if (patient && courseOf(dialog)) url += "&continues=1";
    const result = await get(url);
    if (seq !== quoteSeq) return;
    if (!isOk(result)) { fee.textContent = ""; return; }
    lastFee = result.data.fee == null ? null : result.data.fee * qty();
    // No fee line for someone without money.view: the quote carries no amount (Saged, 2026-11-16).
    fee.textContent = lastFee == null ? "" : t(result.data.is_follow_up ? "fee_follow_up" : "fee", { amount: moneyText(lastFee) });
    if (field("amount") && !field("amount").dataset.touched) field("amount").value = lastFee ? amountInput(lastFee) : "";
  }

  let configCache = null;
  function config() {
    if (!configCache) configCache = JSON.parse(document.getElementById("clinic-config").textContent);
    return configCache;
  }

  // --- saving: one request under one key ---

  function body() {
    const out = {};
    if (patient) out.patient_id = patient.id;
    if (patient && courseOf(dialog)) out.continues_id = courseOf(dialog);
    else if (newBox && !newBox.hidden) out.new_patient = np.values();
    if (confirmOther) out.confirm_other_doctor = true;
    if (field("doctor").value) out.doctor_id = Number(field("doctor").value);
    if (field("service").value) out.service_id = Number(field("service").value);
    if (qty() > 1) out.qty = qty();
    if (field("collect") && field("collect").checked && lastFee !== 0) {  // a free follow-up has nothing to collect
      out.payment = { amount: parsePiastres(field("amount").value), method: form.querySelector('[name="method"]:checked').value };
    }
    return out;
  }

  function check(b) {
    if (!b.patient_id && !b.new_patient) return t("need_patient");
    if (b.new_patient) { const missing = np.problem(b.new_patient); if (missing) return missing; }
    if (b.payment && !b.payment.amount) return t("bad_amount");
    return "";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;  // a double tap does nothing
    const b = submitter.unknown ? submitter.unknown.body : body();
    const problem = submitter.unknown ? "" : check(b);
    if (problem) { submitter.say(problem); return; }
    const who = patient ? patient.name : (b.new_patient && b.new_patient.full_name) || "";
    const result = await submitter.run("POST", "/api/v1/appointments/walk-in", b, [t("walkin"), who].filter(Boolean).join(" · "));
    if (result && !isOk(result)) {
      if (np) np.refused(result);
      showConflict(dialog, result, { onConfirm: () => { confirmOther = true; form.requestSubmit(); } });
    }
    if (!result || !isOk(result)) return;
    const { appointment, turn, ahead } = result.data;
    dialog.close();
    toast(t("walkin_done", { name: appointment.patient.name, n: localDigits(turn), ahead: localDigits(ahead) }));
    if (b.payment && !(appointment.invoice && appointment.invoice.paid >= b.payment.amount)) alertMessage(t("booked_not_paid"));
    if (onBooked) onBooked(result.data);
  });

  // --- opening ---

  // doctorId: the doctor picked at the top of Today (Saged, 2026-11-22): the walk-in is theirs, not asked again.
  function open({ opener, query = "", doctorId = "" } = {}) {
    if (submitter.holdsUnknown()) { openDialog(dialog, opener); return; }  // never a new key over an unknown one
    form.reset();
    submitter.reset();
    results.replaceChildren();
    searchStatus.textContent = "";
    fee.textContent = "";
    if (field("amount")) delete field("amount").dataset.touched;
    if (collectFields) collectFields.hidden = !field("collect").checked;  // a walk-in pays now: ticked (desk review 2)
    lastFee = null;
    if (np) np.reset();
    confirmOther = false;
    hideConflict(dialog);
    patient = null;
    picked.hidden = true;
    finder.hidden = false;
    if (newBox) newBox.hidden = true;
    loadCourses(dialog, null);
    defaults = { doctor_id: null, service_id: null };
    field("doctor").value = doctorId ? String(doctorId) : "";
    if (field("doctor").value !== String(doctorId || "")) field("doctor").value = "";  // not working today
    form.querySelector("[data-doctor-box]").hidden = Boolean(field("doctor").value);
    field("service").value = "";
    filterServices();
    openDialog(dialog, opener);
    field("q").value = query;
    field("q").focus();
    if (query) search();
    loadDefaults(++openSeq);
  }

  return { open };
}
