// The Book dialog (design 7.3): find or add the patient (with the duplicate-phone step), the section in a
// center, the service, the doctor and a slot or a queue turn, and for a walk-in "arrived" and "collect"
// in the same single request (design 5.1).
// With a plan (Saged, 2026-11-13: open({plan: [{id, name, due}]})) the dialog books a patient's follow-up sessions
// in one go: it opens on session 1's suggested day with every session's day marked on the calendar, and after each
// booking moves straight to the next one; "skip" passes one over. Each booking carries its follow-up's id, so the
// server links the two in the one request, and a resend (the same key) or a second person can't book it twice.
import { errorMessage, fetchText, get, isOk } from "./api.js";
import { countOf, courseOf, courseService, loadCourses, showCount, Submitter, wireFamilies, When, closeDialog, el, openDialog, openPrintTab, parseFragment, receiptUrl, wireClose } from "./dialogs.js";
import { amountInput, clockParts, localDigits, moneyText, parsePiastres, phoneText } from "./numbers.js";
import { initNewPatient } from "./newpatient.js";
import { t } from "./strings.js";
import { alertMessage, toast } from "./shell.js";
import { moneyBox } from "./credit.js";

const config = JSON.parse(document.getElementById("clinic-config").textContent);

function clockText(iso) {
  const parts = clockParts(iso);
  return parts ? `${parts.time} ${parts.pm ? t("pm") : t("am")}` : "";
}

// A result element's data (from _patient_results.html or the duplicate-phone list).
export function patientFrom(node) {
  return { id: Number(node.dataset.id), name: node.dataset.name, file: node.dataset.file, phone: node.dataset.phone,
           owed: Number(node.dataset.owed) || 0, credit: Number(node.dataset.credit) || 0 };
}

// "File 12 · 010 1234 5678" as nodes, the phone isolated so its groups keep their order (design 7.2).
export function patientMeta(p) {
  const nodes = [t("file_no", { n: localDigits(p.file) })];
  if (p.phone) {
    const phone = el("bdi", "ltr num", phoneText(p.phone));
    nodes.push(" · ", phone);
  }
  if (p.owed > 0) nodes.push(" · ", el("strong", "owed", t("owed_before", { amount: moneyText(p.owed) })));
  if (p.credit > 0) nodes.push(" · ", el("strong", "credit", t("money_credit", { amount: moneyText(p.credit) })));
  return nodes;
}

// The patient already has a visit (Saged, 2026-11-17): the server's words in the dialog's [data-conflict], with
// "Go to it" for a visit the member may see and, for another doctor's booking that day, "Book anyway", which
// sends again with confirm_other_doctor. Returns true when the answer was one of these.
export function showConflict(dialog, result, { onConfirm } = {}) {
  const box = dialog.querySelector("[data-conflict]");
  const error = result && result.data && result.data.error;
  if (!box) return false;
  box.hidden = true;
  if (!error || !["already_booked", "already_here", "other_doctor_today"].includes(error.code)) return false;
  box.querySelector("[data-conflict-text]").textContent = error.message;
  const go = box.querySelector("[data-conflict-go]");
  const visit = error.appointment || (error.appointments && error.appointments[0]) || null;
  go.hidden = !visit;
  go.onclick = () => goToVisit(dialog, visit);
  const confirm = box.querySelector("[data-conflict-confirm]");
  confirm.hidden = error.code !== "other_doctor_today";
  confirm.onclick = () => { box.hidden = true; if (onConfirm) onConfirm(); };
  box.hidden = false;
  return true;
}

export function hideConflict(dialog) {
  const box = dialog.querySelector("[data-conflict]");
  if (box) box.hidden = true;
}

// Today's row when it is on this page, else Today.
function goToVisit(dialog, visit) {
  closeDialog(dialog);
  const row = visit && document.querySelector(`.appt[data-id="${visit.id}"]`);
  if (!row) { window.location.href = "/"; return; }
  row.scrollIntoView({ block: "center" });
  const button = row.querySelector("button, a");
  if (button) button.focus();
}

export function initBook({ onBooked, defaults } = {}) {
  const dialog = document.getElementById("book-dialog");
  if (!dialog) return null;
  const form = dialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const submitter = new Submitter(dialog, { label: t("booking") });
  const finder = dialog.querySelector("[data-finder]");
  const results = dialog.querySelector("[data-results]");
  const searchStatus = dialog.querySelector("[data-search-status]");
  const picked = dialog.querySelector("[data-picked]");
  const newBox = dialog.querySelector("[data-new]");
  const np = initNewPatient(dialog);
  let confirmOther = false;  // the desk tapped "Book anyway" on another doctor's booking that day
  const fee = dialog.querySelector("[data-fee]");
  const todayOnly = dialog.querySelector("[data-today-only]");
  const collectFields = dialog.querySelector("[data-collect-fields]");
  const submitButton = dialog.querySelector("[data-submit]");
  const depositBox = dialog.querySelector("[data-deposit-box]");
  const money = moneyBox(dialog, form);
  let patient = null;
  let quoteSeq = 0;
  let searchController = null;
  let searchTimer = null;
  let lastFee = null;
  let plan = null;     // {items: [{id, name, due}], index} while a plan's sessions are being booked
  let stepAt = 0;      // when the dialog moved to the next session: a double tap never books that one too
  const planBar = dialog.querySelector("[data-plan-bar]");

  const when = new When(dialog, { onChange: () => { quote(); labels(); } });
  wireClose(dialog);

  // --- a plan's sessions ---

  const session = () => (plan ? plan.items[plan.index] : null);
  const bookable = (iso) => (iso < when.today ? when.today : iso > when.last ? when.last : iso);

  function showPlan() {
    const current = session();
    when.marks = plan ? new Map(plan.items.slice(plan.index).map((item, i) => [bookable(item.due), i ? "later" : "now"]).reverse()) : null;
    const change = dialog.querySelector("[data-change-patient]");
    if (change) change.hidden = Boolean(plan);  // the sessions are this patient's
    if (!planBar) return;
    planBar.hidden = !plan;
    if (!current) return;
    planBar.querySelector("[data-plan-step]").textContent = plan.items.length > 1
      ? t("plan_step", { i: localDigits(plan.index + 1), n: localDigits(plan.items.length), name: current.name }) : current.name;
    planBar.querySelector("[data-plan-hint]").textContent = t("plan_suggested", { day: when.dayLabel(current.due) });
  }

  function nextSession() {
    plan.index += 1;
    stepAt = Date.now();
    start({ patient, doctorId: when.doctor(), sectionId: sectionId(), fixedDoctor: fixed });
  }

  dialog.addEventListener("click", (event) => {
    if (!event.target.closest("[data-plan-skip]") || !plan || dialog.dataset.busy || submitter.holdsUnknown()) return;
    if (plan.index + 1 < plan.items.length) nextSession(); else closeDialog(dialog);
  });

  // --- the patient ---

  function showPicked(p) {
    patient = p;
    confirmOther = false;
    hideConflict(dialog);
    picked.hidden = false;
    finder.hidden = true;
    if (newBox) newBox.hidden = true;
    const name = dialog.querySelector("[data-picked-name]");
    name.replaceChildren(el("bdi", null, p.name));
    name.firstChild.dir = "auto";
    dialog.querySelector("[data-picked-meta]").replaceChildren(...patientMeta(p));
    // A plan's session keeps its own visit fee: nothing is proposed for it (Saged, 2026-11-22).
    loadCourses(dialog, p.id, continues, { propose: !plan }).then(() => { if (patient === p && courseOf(dialog) && !plan) followCourse(); });
    quote();
  }

  function showFinder() {
    loadCourses(dialog, null);
    patient = null;
    picked.hidden = true;
    finder.hidden = false;
    if (newBox) newBox.hidden = true;
    lastFee = null;
    quote();
    field("q").focus();
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
    if (event.key === "ArrowDown") { const first = results.querySelector("[data-pick-patient]"); if (first) { event.preventDefault(); first.focus(); } }
  });
  results.addEventListener("keydown", (event) => {
    const items = [...results.querySelectorAll("[data-pick-patient]")];
    const i = items.indexOf(document.activeElement);
    if (event.key === "ArrowDown" && i < items.length - 1) { event.preventDefault(); items[i + 1].focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); (i > 0 ? items[i - 1] : field("q")).focus(); }
  });
  dialog.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick-patient]");
    if (pick) { event.preventDefault(); showPicked(patientFrom(pick)); }
    if (event.target.closest("[data-change-patient]")) showFinder();
    if (event.target.closest("[data-new-patient]")) showNew();
    if (event.target.closest("[data-back-to-search]")) showFinder();
  });


  // --- section, service, doctor ---

  const sectionId = () => { const c = form.querySelector('[name="section"]:checked'); return c ? c.value : ""; };
  when.sectionOf = sectionId;
  // The consultation chip or an item of the doctor's price list (Saged, 2026-11-22): one of the two is picked.
  const priceList = field("service_list");
  const families = wireFamilies(form, priceList, form.querySelector("[data-price-list]"));
  const pickedService = () => form.querySelector('[name="service"]:checked') || (priceList && priceList.value ? priceList.selectedOptions[0] : null);
  const serviceId = () => { const c = pickedService(); return c ? Number(c.value) : null; };
  function setService(id) {
    const chip = form.querySelector(`[name="service"][value="${CSS.escape(String(id))}"]`);
    const option = chip ? null : families.show(id);
    if (!chip && !option) return false;
    if (chip) families.reset();
    form.querySelectorAll('[name="service"]').forEach((input) => { input.checked = input === chip; });
    if (priceList) priceList.value = option ? option.value : "";
    showQty();
    return true;
  }
  // A visit that goes on with a treatment takes that treatment's service; "A new treatment" goes back to the first.
  function followCourse() {
    const id = courseService(dialog);
    if (!(id && setService(id))) { if (priceList) priceList.value = ""; families.reset(); pickFirstService(); }
    quote();
  }
  // How many (Saged, 2026-11-19, 2026-11-22): 3 fillings are booked once, at 3 times the price; the count follows
  // the service's unit (teeth, quadrants, jaws), and a cleaning or a consultation has none.
  const qtyBox = form.querySelector("[data-qty-box]");
  const qty = () => countOf(qtyBox);
  function showQty() { showCount(qtyBox, pickedService()); }

  function filterBySection() {
    const section = sectionId();
    form.querySelectorAll("[data-services] .chip").forEach((chip) => {
      chip.hidden = Boolean(section) && Boolean(chip.dataset.section) && chip.dataset.section !== section;
      if (chip.hidden) chip.querySelector("input").checked = false;
    });
    if (priceList) {
      for (const o of priceList.options) {
        if (!o.value) continue;
        if (Boolean(section) && Boolean(o.dataset.section) && o.dataset.section !== section) o.dataset.off = "1";
        else delete o.dataset.off;
      }
      families.refresh();
      if (priceList.value && priceList.selectedOptions[0].disabled) priceList.value = "";
    }
    when.filterDoctors(section);
    if (!serviceId()) pickFirstService();
    if (!when.doctor()) pickOnlyDoctor();
  }

  function pickFirstService() {
    const visible = [...form.querySelectorAll("[data-services] .chip:not([hidden]) input")];
    const consult = visible.find((input) => input.dataset.kind === "consultation") || visible[0];
    if (consult) consult.checked = true;
    showQty();
  }

  function pickOnlyDoctor() {
    const visible = [...form.querySelectorAll("[data-doctors] .chip:not([hidden]) input")];
    if (visible.length === 1) visible[0].checked = true;
  }

  form.addEventListener("change", (event) => {
    if (event.target.name === "section") { filterBySection(); when.loadMonth(true); when.refresh(); }
    if (event.target.name === "service") { if (priceList) priceList.value = ""; families.reset(); showQty(); quote(); }
    if (event.target.name === "service_list") {
      // A main service open with no kind picked yet leaves the service empty, not the consultation.
      if (priceList.value || !form.querySelector("[data-price-list]").hidden) form.querySelectorAll('[name="service"]').forEach((input) => { input.checked = false; });
      else pickFirstService();
      showQty(); quote();
    }
    if (event.target.name === "continues_id") followCourse();
    if (event.target.name === "qty") quote();
    if (event.target.name === "collect" || event.target.name === "arrive" || event.target.name === "deposit") labels();
    if (event.target.name === "amount") field("amount").dataset.touched = "1";
    if (event.target.name === "source") { try { localStorage.setItem("hamada.source", event.target.value); } catch (e) { /* none */ } }
    if (event.target.name === "print") { try { localStorage.setItem("hamada.print", field("print").checked ? "1" : ""); } catch (e) { /* none */ } }
  });

  // --- the fee (design 5.1 quote) and the button's words ---

  async function quote() {
    const doctor = when.doctor();
    const service = serviceId();
    const day = when.day();
    const seq = ++quoteSeq;
    if (!doctor || !service || !day) { fee.textContent = ""; money.reset(); return; }
    let url = `/api/v1/appointments/quote?doctor=${doctor}&service=${service}&day=${encodeURIComponent(day)}`;
    if (patient) url += `&patient=${patient.id}`;
    if (patient && courseOf(dialog)) url += "&continues=1";
    if (plan && patient) url += `&follow_up=${session().id}`;  // a plan session's own visit price
    const result = await get(url);
    if (seq !== quoteSeq) return;
    if (!isOk(result)) { fee.textContent = errorMessage(result, ""); return; }
    lastFee = result.data.fee == null ? null : result.data.fee * qty();
    // No fee line for someone without money.view: the quote carries no amount (Saged, 2026-11-16).
    fee.textContent = lastFee == null ? "" : t(result.data.is_follow_up ? "fee_follow_up" : "fee", { amount: moneyText(lastFee) });
    // The fee, plus what is owed from before, less the credit the patient holds (Saged, 2026-11-24).
    const ask = money.set(result.data, lastFee);
    if (field("amount") && !field("amount").dataset.touched) field("amount").value = amountInput(ask);
    labels();
  }

  function isToday() { return when.day() === config.today; }

  function labels() {
    const today = isToday();
    if (todayOnly) todayOnly.hidden = !today;
    const collect = today && field("collect") && field("collect").checked && (lastFee !== 0 || money.owes());
    if (collectFields) collectFields.hidden = !collect;
    const arrive = today && field("arrive") && field("arrive").checked;
    const deposit = takesDeposit(today, collect, arrive);
    if (depositBox) {
      depositBox.hidden = !depositAmount() || collect || arrive;
      depositBox.querySelector("[data-deposit-label]").textContent = t("deposit_amount", { amount: moneyText(depositAmount()) });
      depositBox.querySelector("[data-deposit-methods]").hidden = !deposit;
    }
    submitButton.textContent = collect || deposit ? t("book_collect") : arrive ? t("book_arrive") : t("book");
  }

  // The booking deposit (Saged, 2026-11-16): the clinic's amount, ticked by default. Not with "collect now" (that is
  // the payment) nor for a patient who is here now (they pay at the visit).
  function takesDeposit(today, collect, arrive) {
    return Boolean(depositBox && depositAmount() && !collect && !arrive && field("deposit").checked);
  }

  // A fixed amount, or a percent of the fee shown (to the nearest pound, as the server works it out).
  function depositAmount() {
    if (!config.deposit_percent) return config.deposit;
    return lastFee ? Math.round((lastFee * config.deposit_percent) / 10000) * 100 : 0;
  }

  // --- saving: one request, one key per dialog until a definite answer ---

  function body() {
    const out = { doctor_id: when.doctor(), service_id: serviceId(), day: when.day(), source: form.querySelector('[name="source"]:checked').value };
    if (qty() > 1) out.qty = qty();
    if (patient) out.patient_id = patient.id;
    if (patient && courseOf(dialog)) out.continues_id = courseOf(dialog);
    else if (newBox && !newBox.hidden) out.new_patient = np.values();
    if (confirmOther) out.confirm_other_doctor = true;
    const slot = when.slot();
    if (slot) { out.starts_at = slot; if (when.isExtra()) out.extra = true; }
    if (plan && patient) out.follow_up_id = session().id;
    const today = isToday();
    out.arrive = Boolean(today && field("arrive") && field("arrive").checked);
    const collect = today && field("collect") && field("collect").checked && (lastFee !== 0 || money.owes());
    if (collect) {
      out.payment = money.fill({ amount: parsePiastres(field("amount").value), method: form.querySelector('[name="method"]:checked').value });
    } else if (takesDeposit(today, false, out.arrive)) {
      out.payment = { amount: depositAmount(), method: form.querySelector('[name="deposit_method"]:checked').value, deposit: true };
    }
    return out;
  }

  function check(b) {
    if (!b.patient_id && !b.new_patient) return t("need_patient");
    if (b.new_patient) { const missing = np.problem(b.new_patient); if (missing) return missing; }
    if (!b.service_id) return t("need_service");
    if (!b.doctor_id) return t("need_doctor");
    if (!b.day) return t("need_day");
    if (when.needsTime() && !when.picked()) return t("need_time");
    if (b.payment && b.payment.amount == null) return t("bad_amount");
    if (b.payment && !b.payment.deposit) return money.problem(b.payment);
    return "";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;  // a double tap does nothing
    if (Date.now() - stepAt < 800) return;  // nor does it book the plan's next session
    const b = submitter.unknown ? submitter.unknown.body : body();
    const problem = submitter.unknown ? "" : check(b);
    if (problem) { submitter.say(problem); return; }
    const printing = Boolean(b.payment && isToday() && field("print") && field("print").checked);
    const tab = printing ? openPrintTab() : null;  // opened in the tap, where Safari allows it
    const who = patient ? patient.name : (b.new_patient && b.new_patient.full_name) || "";
    const label = [t("booking"), who, b.payment ? moneyText(b.payment.amount) : ""].filter(Boolean).join(" · ");
    const result = await submitter.run("POST", "/api/v1/appointments", b, label);
    if (result && !isOk(result)) {
      depositAnswer(result);
      money.refused(result);
      if (np) np.refused(result);
      showConflict(dialog, result, { onConfirm: () => { confirmOther = true; form.requestSubmit(); } });
    }
    if (!result || !isOk(result)) { if (tab) tab.close(); return; }
    const appointment = result.data.appointment;
    const bookedSession = b.follow_up_id ? session() : null;
    const more = Boolean(bookedSession && plan.index + 1 < plan.items.length);
    // "Collected" only when the answer's invoice shows the money (M1 decision R2), never from what was sent.
    // Part of it may have paid older bills: the visit's own bill shows at least its share (Saged, 2026-11-24).
    const paid = Boolean(b.payment && appointment.invoice && appointment.invoice.paid > 0);
    if (tab && paid) tab.location.href = receiptUrl(appointment.invoice.id);
    else if (tab) tab.close();
    if (!more) dialog.close();
    // The toast says when: the day and the time, or the turn on a queue day (Saged, 2026-11-13).
    const names = { name: appointment.patient.name, n: localDigits(appointment.queue_no), day: when.dayLabel(appointment.day),
                    time: appointment.starts_at ? clockText(appointment.starts_at) : "" };
    const bookedKey = appointment.starts_at ? "booked_at" : appointment.day !== config.today ? "booked_day" : "booked";
    if (paid) {
      toast(t("booked_paid", { ...names, amount: moneyText(appointment.invoice.paid) }),
        printing && !tab ? { label: t("print_receipt"), href: receiptUrl(appointment.invoice.id) } : null);
    } else {
      toast(t(bookedKey, names));
      if (b.payment) alertMessage(t("booked_not_paid"));
    }
    if (onBooked) onBooked(result.data, bookedSession);
    if (more) nextSession();  // straight on to the plan's next session
  });

  // The deposit was turned off or changed meanwhile: the dialog shows the clinic's setting now (nothing was booked).
  function depositAnswer(result) {
    const error = result.data && result.data.error;
    if (!error || !depositBox) return;
    if (error.code === "deposit_off" && !config.deposit_percent) config.deposit = 0;
    else if (error.code === "deposit_changed" && Number.isInteger(error.amount)) config.deposit = error.amount;
    else return;
    labels();
  }

  // --- opening ---

  let fixed = null;  // the doctor picked at the top of Today, kept for the plan's next sessions
  let continues = null;  // the visit whose treatment a "next visit" continues
  function open({ plan: sessions = null, opener, ...rest } = {}) {
    if (submitter.holdsUnknown()) { openDialog(dialog, opener); return; }  // never a new key over an unknown one (R7)
    plan = sessions && sessions.length && rest.patient ? { items: sessions, index: 0 } : null;
    stepAt = 0;
    start({ ...rest, opener });
  }

  // The dialog afresh, for a new booking or for the plan's next session (the dialog then stays open).
  // fixedDoctor: the doctor picked at the top of Today (Saged, 2026-11-22): the booking is theirs, not asked again.
  function start({ query = "", patient: given = null, doctorId = null, sectionId: section = "", opener, fixedDoctor = null,
                   continues: course = null } = {}) {
    continues = course;  // "Book the next visit" from a row: that visit's treatment goes on
    form.reset();
    loadCourses(dialog, null);
    submitter.reset();
    results.replaceChildren();
    searchStatus.textContent = "";
    if (np) np.reset();
    confirmOther = false;
    hideConflict(dialog);
    if (field("amount")) delete field("amount").dataset.touched;
    showCount(qtyBox, null);
    families.reset();
    if (field("print")) { try { field("print").checked = localStorage.getItem("hamada.print") === "1"; } catch (e) { /* none */ } }
    // "Booked by" opens on the last one this browser used (desk review 6); "here now" and "collect" start unticked.
    try {
      const source = form.querySelector(`[name="source"][value="${CSS.escape(localStorage.getItem("hamada.source") || "")}"]`);
      if (source) source.checked = true;
    } catch (e) { /* none */ }
    when.reset();
    if (plan) when.setDay(bookable(session().due));  // the session's suggested day; an overdue one from today on
    showPlan();
    lastFee = null;
    const d = defaults ? defaults() : {};
    section = section || d.sectionId || "";
    if (section) { const s = form.querySelector(`[name="section"][value="${CSS.escape(String(section))}"]`); if (s) s.checked = true; }
    filterBySection();
    const doctorBox = form.querySelector("[data-doctor-box]");
    doctorBox.hidden = false;  // setDoctor skips hidden choices
    when.setDoctor(fixedDoctor || doctorId || d.doctorId || "");
    if (!when.doctor()) pickOnlyDoctor();
    fixed = fixedDoctor && when.doctor() === Number(fixedDoctor) ? Number(fixedDoctor) : null;
    doctorBox.hidden = Boolean(fixed);
    if (given) showPicked(given);
    else { patient = null; picked.hidden = true; finder.hidden = false; if (newBox) newBox.hidden = true; field("q").value = query; }
    openDialog(dialog, opener || dialog._opener);
    if (!given) { field("q").focus(); if (query) search(); }
    when.loadMonth(true);
    when.refresh();
    quote();
    labels();
  }

  return { open };
}
