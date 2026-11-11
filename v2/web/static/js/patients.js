// Patients (design 5.7, 7.3): search as you type, the patient file with "load more", editing a file,
// booking for this patient and taking a payment on a visit.
import { errorMessage, fetchText, isOk } from "./api.js";
import { initBook, patientFrom } from "./book.js";
import { Submitter, openDialog, parseFragment, receiptUrl, wireClose } from "./dialogs.js";
import { asciiDigits, localDigits, parseWhole } from "./numbers.js";
import { initPay } from "./pay.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

// --- search: 250 ms debounce, at least 2 characters, the previous request aborted ---

const searchForm = document.querySelector("[data-search]");
if (searchForm) {
  const input = searchForm.elements.namedItem("q");
  const results = document.querySelector("[data-results]");
  const status = document.querySelector("[data-search-status]");
  let controller = null;
  let timer = null;
  async function search() {
    const q = input.value.trim();
    if (controller) controller.abort();
    const url = new URL(window.location.href);
    if (q) url.searchParams.set("q", q); else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
    if (q.length < 2) { status.textContent = ""; return; }
    controller = new AbortController();
    status.textContent = t("searching");
    const result = await fetchText(`/patients/search?q=${encodeURIComponent(q)}`, { signal: controller.signal });
    if (result.aborted) return;
    if (result.status !== 200) { status.textContent = errorMessage(result, t("search_failed")); return; }
    results.replaceChildren(parseFragment(result.text));
    const n = results.querySelectorAll(".result").length;
    status.textContent = n ? t("results", { n: localDigits(n) }) : "";
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 250); });
  searchForm.addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(timer); search(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { const first = results.querySelector(".result"); if (first) { event.preventDefault(); first.focus(); } }
  });
}

// --- new or edited patient file: one request, one key until a definite answer ---

const patientDialog = document.getElementById("patient-dialog");
const patientData = document.getElementById("patient-data");
const current = patientData ? JSON.parse(patientData.textContent) : null;

if (patientDialog) {
  const form = patientDialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const submitter = new Submitter(patientDialog, { label: t("patient_saved", { name: "" }) });
  const TEXT = ["full_name", "address", "allergies", "chronic_conditions", "notes"];
  let editing = false;
  wireClose(patientDialog);

  function values() {
    const sex = form.querySelector('[name="sex"]:checked');
    const out = { phone: asciiDigits(field("phone").value).trim(), sex: sex ? sex.value : "" };
    for (const name of TEXT) out[name] = field(name).value.trim();
    const age = field("age").value.trim();
    if (age) out.age = parseWhole(age);
    else out.birth_date = field("birth_date").value || null;
    return out;
  }

  function open(edit, opener) {
    editing = edit;
    form.reset();
    submitter.reset();
    patientDialog.querySelector("[data-title-new]").hidden = edit;
    patientDialog.querySelector("[data-title-edit]").hidden = !edit;
    if (edit && current) {
      for (const name of TEXT) field(name).value = current[name] || "";
      field("phone").value = current.phone || "";
      const sex = form.querySelector(`[name="sex"][value="${current.sex}"]`);
      if (sex) sex.checked = true;
      if (current.birth_date_estimated) field("age").value = current.age ?? "";
      else field("birth_date").value = current.birth_date || "";
    }
    openDialog(patientDialog, opener);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (patientDialog.dataset.busy) return;
    let method = "POST";
    let url = "/api/v1/patients";
    let body;
    if (submitter.unknown) {
      ({ method, url, body } = submitter.unknown);
    } else {
      const v = values();
      if (v.full_name.length < 2) { submitter.say(t("need_name")); return; }
      if (field("age").value.trim() && v.age === null) { submitter.say(t("bad_age")); return; }
      if (editing) {
        // Absolute values with the values they replace (design 5.0): only what changed is sent.
        const changes = {};
        const expected = {};
        for (const name of [...TEXT, "phone", "sex"]) {
          if ((v[name] || "") !== (current[name] || "")) { changes[name] = v[name]; expected[name] = current[name] || ""; }
        }
        if ("age" in v && !(current.birth_date_estimated && v.age === current.age)) { changes.age = v.age; expected.birth_date = current.birth_date; }
        if ("birth_date" in v && v.birth_date !== (current.birth_date_estimated ? current.birth_date : current.birth_date || null)) {
          changes.birth_date = v.birth_date || ""; expected.birth_date = current.birth_date;
        }
        if (!Object.keys(changes).length) { patientDialog.close(); return; }
        method = "PATCH";
        url = `/api/v1/patients/${current.id}`;
        body = { changes, expected };
      } else {
        body = { ...v };
        if (body.birth_date === null) delete body.birth_date;
      }
    }
    const result = await submitter.run(method, url, body, t("patient_saved", { name: body.full_name || (body.changes && body.changes.full_name) || "" }));
    if (!result || !isOk(result)) return;
    const saved = result.data.patient;
    patientDialog.close();
    if (editing) window.location.reload();
    else window.location.href = `/patients/${saved.id}`;
  });

  document.addEventListener("click", (event) => {
    const add = event.target.closest("[data-new-patient-file]");
    const edit = event.target.closest("[data-edit-patient]");
    if (add) open(false, add);
    if (edit) open(true, edit);
  });
}

// --- the patient file: book, pay, print, load more ---

if (current) {
  const book = initBook({ onBooked: () => window.location.reload() });
  const pay = initPay({ onChanged: () => { reloadVisitsSoon = true; } });
  let reloadVisitsSoon = false;
  const payDialog = document.getElementById("pay-dialog");
  if (payDialog) payDialog.addEventListener("close", () => { if (reloadVisitsSoon) window.location.reload(); });

  document.addEventListener("click", async (event) => {
    const bookButton = event.target.closest("[data-book-patient]");
    if (bookButton && book) book.open({ patient: patientFrom(bookButton), opener: bookButton });
    const act = event.target.closest("[data-act]");
    if (act) {
      const row = act.closest("[data-visit]");
      const visit = {
        id: Number(row.dataset.id), status: row.dataset.status, due: Number(row.dataset.due), fee: Number(row.dataset.fee),
        invoiceId: row.dataset.invoice ? Number(row.dataset.invoice) : null, name: current.full_name, service: row.dataset.service,
      };
      if (act.dataset.act === "pay" && pay) pay.open(visit);
      if (act.dataset.act === "receipt" && visit.invoiceId) window.open(receiptUrl(visit.invoiceId), "_blank");
    }
    const more = event.target.closest("[data-more]");
    if (more && !more.disabled) {
      more.disabled = true;
      const kind = more.dataset.more;
      const result = await fetchText(`/patients/${current.id}/${kind}?after=${encodeURIComponent(more.dataset.next)}`);
      more.disabled = false;
      if (result.status !== 200) { alertMessage(errorMessage(result, t("failed"))); return; }
      document.querySelector(`[data-rows="${kind}"]`).append(parseFragment(result.text));
      const count = document.querySelector(`[data-count="${kind}"]`);
      count.dataset.n = Number(count.dataset.n) + Number(result.headers.get("X-Count") || 0);
      count.textContent = t("showing", { n: localDigits(count.dataset.n) });
      const next = result.headers.get("X-Next");
      if (next) more.dataset.next = next; else more.remove();
    }
  });
}

if (new URLSearchParams(window.location.search).get("saved")) toast(t("saved"));
