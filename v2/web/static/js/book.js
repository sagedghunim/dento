// The Book dialog (design 7.3): find or add the patient (with the duplicate-phone step), the section in a
// center, the service, the doctor and a slot or a queue turn, and for a walk-in "arrived" and "collect"
// in the same single request (design 5.1).
import { errorMessage, fetchText, get, isOk } from "./api.js";
import { Submitter, When, el, openDialog, openPrintTab, parseFragment, receiptUrl, wireClose } from "./dialogs.js";
import { amountInput, asciiDigits, localDigits, moneyText, parsePiastres, parseWhole, phoneText } from "./numbers.js";
import { t } from "./strings.js";
import { toast } from "./shell.js";

const config = JSON.parse(document.getElementById("clinic-config").textContent);

// A result element's data (from _patient_results.html or the duplicate-phone list).
export function patientFrom(node) {
  return { id: Number(node.dataset.id), name: node.dataset.name, file: node.dataset.file, phone: node.dataset.phone };
}

// "File 12 · 010 1234 5678" as nodes, the phone isolated so its groups keep their order (design 7.2).
export function patientMeta(p) {
  const nodes = [t("file_no", { n: localDigits(p.file) })];
  if (p.phone) {
    const phone = el("bdi", "ltr num", phoneText(p.phone));
    nodes.push(" · ", phone);
  }
  return nodes;
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
  const dupes = dialog.querySelector("[data-dupes]");
  const fee = dialog.querySelector("[data-fee]");
  const todayOnly = dialog.querySelector("[data-today-only]");
  const collectFields = dialog.querySelector("[data-collect-fields]");
  const submitButton = dialog.querySelector("[data-submit]");
  let patient = null;
  let quoteSeq = 0;
  let searchController = null;
  let searchTimer = null;
  let lastFee = null;

  const when = new When(dialog, { onChange: () => { quote(); labels(); } });
  wireClose(dialog);

  // --- the patient ---

  function showPicked(p) {
    patient = p;
    picked.hidden = false;
    finder.hidden = true;
    if (newBox) newBox.hidden = true;
    const name = dialog.querySelector("[data-picked-name]");
    name.replaceChildren(el("bdi", null, p.name));
    name.firstChild.dir = "auto";
    dialog.querySelector("[data-picked-meta]").replaceChildren(...patientMeta(p));
    quote();
  }

  function showFinder() {
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
    const q = field("q").value.trim();
    if (q && !field("full_name").value && !/^[\d\s+٠-٩۰-۹-]+$/.test(q)) field("full_name").value = q;
    if (q && !field("phone").value && /^[\d\s+٠-٩۰-۹-]+$/.test(q)) field("phone").value = q;
    field("full_name").focus();
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

  // Booking a new name with a phone that already has a file suggests that file; it never blocks (R4.2).
  async function checkPhone() {
    const digits = asciiDigits(field("phone").value).replace(/\D/g, "");
    dupes.hidden = true;
    if (digits.length < 10) return;
    const result = await get(`/api/v1/patients/by-phone?phone=${encodeURIComponent(digits)}`);
    if (!isOk(result) || !result.data.items.length) return;
    const list = dialog.querySelector("[data-dupe-list]");
    list.replaceChildren(...result.data.items.map((p) => {
      const button = el("button", "result");
      button.type = "button";
      Object.assign(button.dataset, { pickPatient: "", id: p.id, name: p.name, file: p.file_no, phone: p.phone || "" });
      const who = el("bdi", "who", p.name);
      who.dir = "auto";
      const meta = el("span", "meta");
      meta.append(...patientMeta({ file: p.file_no, phone: p.phone }));
      button.append(who, meta);
      return button;
    }));
    dupes.hidden = false;
  }
  if (newBox) field("phone").addEventListener("change", checkPhone);

  // --- section, service, doctor ---

  const sectionId = () => { const c = form.querySelector('[name="section"]:checked'); return c ? c.value : ""; };
  when.sectionOf = sectionId;
  const serviceId = () => { const c = form.querySelector('[name="service"]:checked'); return c ? Number(c.value) : null; };

  function filterBySection() {
    const section = sectionId();
    form.querySelectorAll("[data-services] .chip").forEach((chip) => {
      chip.hidden = Boolean(section) && Boolean(chip.dataset.section) && chip.dataset.section !== section;
      if (chip.hidden) chip.querySelector("input").checked = false;
    });
    when.filterDoctors(section);
    if (!serviceId()) pickFirstService();
    if (!when.doctor()) pickOnlyDoctor();
  }

  function pickFirstService() {
    const visible = [...form.querySelectorAll("[data-services] .chip:not([hidden]) input")];
    const consult = visible.find((input) => input.dataset.kind === "consultation") || visible[0];
    if (consult) consult.checked = true;
  }

  function pickOnlyDoctor() {
    const visible = [...form.querySelectorAll("[data-doctors] .chip:not([hidden]) input")];
    if (visible.length === 1) visible[0].checked = true;
  }

  form.addEventListener("change", (event) => {
    if (event.target.name === "section") { filterBySection(); when.refresh(); }
    if (event.target.name === "service") quote();
    if (event.target.name === "collect" || event.target.name === "arrive") labels();
    if (event.target.name === "amount") field("amount").dataset.touched = "1";
    if (event.target.name === "print") { try { localStorage.setItem("hamada.print", field("print").checked ? "1" : ""); } catch (e) { /* none */ } }
  });

  // --- the fee (design 5.1 quote) and the button's words ---

  async function quote() {
    const doctor = when.doctor();
    const service = serviceId();
    const day = when.day();
    const seq = ++quoteSeq;
    if (!doctor || !service || !day) { fee.textContent = ""; return; }
    let url = `/api/v1/appointments/quote?doctor=${doctor}&service=${service}&day=${encodeURIComponent(day)}`;
    if (patient) url += `&patient=${patient.id}`;
    const result = await get(url);
    if (seq !== quoteSeq) return;
    if (!isOk(result)) { fee.textContent = errorMessage(result, ""); return; }
    lastFee = result.data.fee;
    fee.textContent = t(result.data.is_follow_up ? "fee_follow_up" : "fee", { amount: moneyText(result.data.fee) });
    if (field("amount") && !field("amount").dataset.touched) field("amount").value = amountInput(result.data.fee);
    labels();
  }

  function isToday() { return when.day() === config.today; }

  function labels() {
    const today = isToday();
    if (todayOnly) todayOnly.hidden = !today;
    const collect = today && field("collect") && field("collect").checked && lastFee !== 0;
    if (collectFields) collectFields.hidden = !collect;
    const arrive = today && field("arrive") && field("arrive").checked;
    submitButton.textContent = collect ? t("book_collect") : arrive ? t("book_arrive") : t("book");
  }

  // --- saving: one request, one key per dialog until a definite answer ---

  function body() {
    const out = { doctor_id: when.doctor(), service_id: serviceId(), day: when.day(), source: form.querySelector('[name="source"]:checked').value };
    if (patient) out.patient_id = patient.id;
    else if (newBox && !newBox.hidden) {
      const sex = form.querySelector('[name="sex"]:checked');
      out.new_patient = { full_name: field("full_name").value.trim(), phone: asciiDigits(field("phone").value).trim(), sex: sex ? sex.value : "" };
      const age = field("age").value.trim();
      if (age) out.new_patient.age = parseWhole(age);
    }
    const slot = when.slot();
    if (slot) { out.starts_at = slot; if (when.isExtra()) out.extra = true; }
    const today = isToday();
    out.arrive = Boolean(today && field("arrive") && field("arrive").checked);
    if (today && field("collect") && field("collect").checked && lastFee !== 0) {
      out.payment = { amount: parsePiastres(field("amount").value), method: form.querySelector('[name="method"]:checked').value };
    }
    return out;
  }

  function check(b) {
    if (!b.patient_id && !b.new_patient) return t("need_patient");
    if (b.new_patient && b.new_patient.full_name.length < 2) return t("need_name");
    if (b.new_patient && field("age").value.trim() && b.new_patient.age === null) return t("bad_age");
    if (!b.service_id) return t("need_service");
    if (!b.doctor_id) return t("need_doctor");
    if (!b.day) return t("need_day");
    if (when.needsTime() && !b.starts_at) return t("need_time");
    if (b.payment && !b.payment.amount) return t("bad_amount");
    return "";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;  // a double tap does nothing
    const b = submitter.unknown ? submitter.unknown.body : body();
    const problem = submitter.unknown ? "" : check(b);
    if (problem) { submitter.say(problem); return; }
    const printing = Boolean(b.payment && field("print") && field("print").checked);
    const tab = printing ? openPrintTab() : null;  // opened in the tap, where Safari allows it
    const result = await submitter.run("POST", "/api/v1/appointments", b, t("booking"));
    if (!result || !isOk(result)) { if (tab) tab.close(); return; }
    const appointment = result.data.appointment;
    if (tab && appointment.invoice) tab.location.href = receiptUrl(appointment.invoice.id);
    dialog.close();
    toast(t("booked", { name: appointment.patient.name, n: appointment.queue_no }),
      printing && !tab && appointment.invoice ? { label: t("print_receipt"), href: receiptUrl(appointment.invoice.id) } : null);
    if (onBooked) onBooked(result.data);
  });

  // --- opening ---

  function open({ query = "", patient: given = null, doctorId = null, sectionId: section = "", opener } = {}) {
    form.reset();
    submitter.reset();
    results.replaceChildren();
    searchStatus.textContent = "";
    if (dupes) dupes.hidden = true;
    if (field("amount")) delete field("amount").dataset.touched;
    if (field("print")) { try { field("print").checked = localStorage.getItem("hamada.print") === "1"; } catch (e) { /* none */ } }
    when.otherWrap.hidden = true;
    lastFee = null;
    const d = defaults ? defaults() : {};
    section = section || d.sectionId || "";
    if (section) { const s = form.querySelector(`[name="section"][value="${CSS.escape(String(section))}"]`); if (s) s.checked = true; }
    filterBySection();
    when.setDoctor(doctorId || d.doctorId || "");
    if (!when.doctor()) pickOnlyDoctor();
    if (given) showPicked(given);
    else { patient = null; picked.hidden = true; finder.hidden = false; if (newBox) newBox.hidden = true; field("q").value = query; }
    openDialog(dialog);
    if (!given) { field("q").focus(); if (query) search(); }
    when.refresh();
    quote();
    labels();
  }

  return { open };
}
