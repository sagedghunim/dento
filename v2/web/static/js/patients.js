// Patients (design 5.7, 7.3): search as you type, the patient file with "load more", editing a file,
// booking for this patient and taking a payment on a visit.
import { errorMessage, fetchText, isOk, membership } from "./api.js";
import { initBook, patientFrom } from "./book.js";
import { Submitter, openDialog, parseFragment, receiptUrl, wireClose } from "./dialogs.js";
import { localDigits } from "./numbers.js";
import { initNewPatient } from "./newpatient.js";
import { initPay } from "./pay.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

// --- search: 250 ms debounce, at least 2 characters, the previous request aborted ---

const searchForm = document.querySelector("[data-search]");
if (searchForm) {
  const input = searchForm.elements.namedItem("q");
  const results = document.querySelector("[data-results]");
  const status = document.querySelector("[data-search-status]");
  const firstList = input.value.trim() ? null : [...results.childNodes];  // the patients seen lately
  let controller = null;
  let timer = null;
  async function search() {
    const q = input.value.trim();
    if (controller) controller.abort();
    const url = new URL(window.location.href);
    if (q) url.searchParams.set("q", q); else url.searchParams.delete("q");
    window.history.replaceState(null, "", url);
    if (q.length < 2) {
      status.textContent = "";
      if (!q && firstList) results.replaceChildren(...firstList);
      return;
    }
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
  const TEXT = ["address", "allergies", "chronic_conditions", "notes"];
  const IDENTITY = ["full_name", "phone", "phone_owner", "guardian_relation", "sex"];
  const np = initNewPatient(patientDialog);
  let editing = false;
  wireClose(patientDialog);

  function values() {
    const out = np.values();
    for (const name of TEXT) out[name] = field(name).value.trim();
    if (out.age == null) out.birth_date = field("birth_date").value || null;
    return out;
  }

  function open(edit, opener) {
    if (submitter.holdsUnknown()) { openDialog(patientDialog, opener); return; }
    editing = edit;
    form.reset();
    submitter.reset();
    np.reset();
    patientDialog.querySelector("[data-title-new]").hidden = edit;
    patientDialog.querySelector("[data-title-edit]").hidden = !edit;
    if (edit && current) {
      for (const name of TEXT) field(name).value = current[name] || "";
      np.fill(current);
      if (!current.birth_date_estimated) field("birth_date").value = current.birth_date || "";
    }
    // The desk fills the medical fields on a new file only; after that they are the doctor's (Saged, 2026-11-22).
    const locked = edit && patientDialog.hasAttribute("data-medical-locked");
    patientDialog.querySelectorAll("[data-medical]").forEach((label) => { label.hidden = locked; });
    openDialog(patientDialog, opener);
  }

  // A suggested file (a name or a number already on file) opens that file instead of adding a new one.
  patientDialog.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick-patient]");
    if (pick) { event.preventDefault(); window.location.href = `/patients/${pick.dataset.id}`; }
  });

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
      const missing = np.problem(v);
      if (missing) { submitter.say(missing); return; }
      if (editing) {
        // Absolute values with the values they replace (design 5.0): only what changed is sent.
        const changes = {};
        const expected = {};
        for (const name of [...TEXT, ...IDENTITY]) {
          if ((v[name] || "") !== (current[name] || "")) { changes[name] = v[name]; expected[name] = current[name] || ""; }
        }
        if ("phone_owner" in changes || "guardian_relation" in changes) {  // the two go together
          changes.phone_owner = v.phone_owner; changes.guardian_relation = v.guardian_relation || "";
          expected.phone_owner = current.phone_owner; expected.guardian_relation = current.guardian_relation || "";
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
    if (result && !isOk(result)) np.refused(result);
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

// --- an x-ray or file (Saged, 2026-11-17): read with the clinic's header, shown in a new tab ---

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-open-file]");
  if (!button || button.disabled) return;
  const tab = window.open("", "_blank");  // opened in the click, so no pop-up blocker stops it
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.openFile, { headers: { "X-Membership": membership() }, credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    const url = URL.createObjectURL(await response.blob());
    if (tab) tab.location.href = url; else window.location.href = url;
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    if (tab) tab.close();
    alertMessage(t("file_failed"));
  } finally {
    button.disabled = false;
  }
});

// --- installments (Saged, 2026-11-19): split what is owed; the server makes the parts and checks them ---
import("./installments.js");

// --- the owner brings back a file hidden before deleting was taken out (Saged, 2026-11-26: no deleting a
// patient at all; one nobody wants goes on the blacklist instead) ---
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-restore-patient]");
  if (!button || button.disabled) return;
  button.disabled = true;
  const { isOk: ok, newKey: key, save: send, errorMessage: message } = await import("./api.js");
  const url = `/api/v1/patients/${button.dataset.id}/restore`;
  const result = await send("POST", url, {}, { key: key(), label: button.dataset.name });
  if (result.settled && ok(result)) {
    toast(t("patient_restored", { name: button.dataset.name }));
    button.closest("li").remove();
    return;
  }
  button.disabled = false;
  alertMessage(message(result, t("failed")));
});

// --- the patient's tags (Saged, 2026-11-19): one PUT with the ticked ones; a new tag is one POST first ---
const tagsDialog = document.getElementById("tags-dialog");
if (tagsDialog) {
  wireClose(tagsDialog);
  const tagForm = tagsDialog.querySelector("form");
  const tagError = tagsDialog.querySelector("[data-error]");
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-tags]")) openDialog(tagsDialog, event.target);
  });
  const add = tagsDialog.querySelector("[data-add-tag]");
  if (add) {
    add.addEventListener("click", async () => {
      const input = tagForm.elements.namedItem("new_tag");
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      add.disabled = true;
      const { isOk: ok, newKey: key, save: send } = await import("./api.js");
      const result = await send("POST", "/api/v1/patient-tags", { name }, { key: key(), label: name });
      add.disabled = false;
      if (!(result.settled && ok(result))) { tagError.textContent = errorMessage(result, t("failed")); return; }
      const tag = result.data.tag;
      const label = document.createElement("label");
      label.className = "chip";
      label.innerHTML = '<input type="checkbox" name="tag" checked><span><bdi dir="auto"></bdi></span>';
      label.querySelector("input").value = tag.id;
      label.querySelector("bdi").textContent = tag.name;
      tagsDialog.querySelector("[data-tag-chips]").append(label);
      input.value = "";
    });
  }
  tagForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ids = [...tagForm.querySelectorAll('[name="tag"]:checked')].map((c) => Number(c.value));
    const { isOk: ok, newKey: key, save: send } = await import("./api.js");
    const button = tagForm.querySelector("[data-submit]");
    button.disabled = true;
    const result = await send("PUT", `/api/v1/patients/${tagsDialog.dataset.patient}/tags`, { tag_ids: ids }, { key: key(), label: t("tags_title") });
    button.disabled = false;
    if (result.settled && ok(result)) { window.location.reload(); return; }
    tagError.textContent = errorMessage(result, t("failed"));
  });
}
