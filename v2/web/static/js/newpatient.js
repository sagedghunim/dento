// A new patient's fields (Saged, 2026-11-17), shared by Book, Walk-in and the Patients page (_new_patient.html):
// a three-part name with this clinic's files that match it offered while typing, a mobile with whose number it is
// (the patient's own: one file; a guardian's: siblings may share it), the sex in one tap and an age. Under 16 the
// number is proposed as a guardian's. The server checks it all again (patients.services); this only saves a trip.
import { fetchText, get, isOk } from "./api.js";
import { el, parseFragment } from "./dialogs.js";
import { asciiDigits, localDigits, parseWhole, phoneText } from "./numbers.js";

const GUARDIAN_AGE = 16;
const EGYPT_MOBILE = /^01[0125]\d{8}$/;

// "+20 10 1234 5678", "٠١٠١٢٣٤٥٦٧٨" and "1012345678" as the server stores them (core.text.normalize_phone).
export function phoneDigits(value) {
  let digits = asciiDigits(value || "").replace(/\D/g, "");
  if (digits.startsWith("0020")) digits = `0${digits.slice(4)}`;
  else if (digits.startsWith("20") && digits.length === 12) digits = `0${digits.slice(2)}`;
  else if (digits.length === 10 && digits.startsWith("1")) digits = `0${digits}`;
  return digits;
}

// A file as a pick button, like a search result (book.js patientFrom reads its data-*).
function pickButton(p, label) {
  const button = el("button", "result");
  button.type = "button";
  Object.assign(button.dataset, { pickPatient: "", id: p.id, name: p.name, file: p.file_no, phone: p.phone || "" });
  const who = el("bdi", "who", p.name);
  who.dir = "auto";
  const meta = el("span", "meta");
  const phone = el("bdi", "ltr num", phoneText(p.phone || ""));
  meta.append(phone);
  if (p.age != null) meta.append(` · ${label("age_short")} ${localDigits(p.age)}`);
  button.append(who, meta);
  return button;
}

export function initNewPatient(root) {
  const box = root.querySelector("[data-np]");
  if (!box) return null;
  const field = (name) => box.querySelector(`[name="${name}"]`);
  const strings = Object.fromEntries([...box.querySelectorAll("[data-np-strings] [data-k]")].map((s) => [s.dataset.k, s.textContent]));
  const label = (key) => strings[key] || key;
  const suggest = box.querySelector("[data-np-suggest]");
  const suggestList = box.querySelector("[data-np-suggest-list]");
  const relation = box.querySelector("[data-np-relation]");
  const notice = box.querySelector("[data-np-phone]");
  const noticeText = box.querySelector("[data-np-phone-text]");
  const noticeList = box.querySelector("[data-np-phone-list]");
  const newChild = box.querySelector("[data-np-new-child]");
  let ownerTouched = false;
  let nameTimer = null;
  let nameController = null;
  let phoneSeq = 0;
  let onNumber = null;  // the last by-phone answer for the typed number
  let quiet = false;     // editing a file: no suggestions of itself

  const checked = (name) => { const c = box.querySelector(`[name="${name}"]:checked`); return c ? c.value : ""; };
  const owner = () => checked("phone_owner") || "self";

  function setOwner(value) {
    const input = box.querySelector(`[name="phone_owner"][value="${value}"]`);
    if (input) input.checked = true;
    relation.hidden = value !== "guardian";
    showNumber();
  }

  // --- names already on file, while typing (the clinic's search: spelling slips tolerated on the server) ---

  async function suggestNames() {
    const q = field("full_name").value.trim();
    if (nameController) nameController.abort();
    if (q.length < 3 || quiet) { suggest.hidden = true; suggestList.replaceChildren(); return; }
    nameController = new AbortController();
    const result = await fetchText(`/patients/search?pick=1&q=${encodeURIComponent(q)}`, { signal: nameController.signal });
    if (result.aborted || result.status !== 200) return;
    suggestList.replaceChildren(parseFragment(result.text));
    suggestList.querySelectorAll(".empty-note").forEach((n) => n.remove());
    suggest.hidden = !suggestList.querySelector("[data-pick-patient]");
  }

  // --- the number: whose it is, and the files already on it ---

  function showNumber() {
    const answer = onNumber;
    notice.hidden = true;
    newChild.hidden = true;
    if (!answer) return;
    const own = answer.items.filter((p) => p.phone_owner === "self");
    if (owner() === "self" && answer.taken) {
      noticeText.textContent = own.length ? label("taken") : label("taken_hidden");
      noticeList.replaceChildren(...own.map((p) => pickButton(p, label)));
      notice.hidden = false;
    } else if (owner() === "guardian" && answer.items.length) {
      noticeText.textContent = label("on_number");
      noticeList.replaceChildren(...answer.items.map((p) => pickButton(p, label)));
      newChild.hidden = false;
      notice.hidden = false;
    }
  }

  async function checkNumber() {
    const digits = phoneDigits(field("phone").value);
    const seq = ++phoneSeq;
    onNumber = null;
    showNumber();
    if (!EGYPT_MOBILE.test(digits) || quiet) return;
    const result = await get(`/api/v1/patients/by-phone?phone=${encodeURIComponent(digits)}`);
    if (seq !== phoneSeq || !isOk(result)) return;
    onNumber = result.data;
    showNumber();
  }

  field("full_name").addEventListener("input", () => { clearTimeout(nameTimer); nameTimer = setTimeout(suggestNames, 250); });
  field("phone").addEventListener("input", () => { if (phoneDigits(field("phone").value).length >= 11) checkNumber(); else { onNumber = null; showNumber(); } });
  field("phone").addEventListener("change", checkNumber);
  box.addEventListener("change", (event) => {
    if (event.target.name === "phone_owner") { ownerTouched = true; setOwner(event.target.value); }
  });
  field("age").addEventListener("input", () => {
    const age = parseWhole(field("age").value.trim());
    if (!ownerTouched && age !== null) setOwner(age < GUARDIAN_AGE ? "guardian" : "self");
  });
  newChild.addEventListener("click", () => { notice.hidden = true; field("sex").focus(); });

  return {
    reset() {
      ["full_name", "phone", "age"].forEach((n) => { field(n).value = ""; });
      box.querySelectorAll('[name="sex"], [name="guardian_relation"]').forEach((i) => { i.checked = false; });
      ownerTouched = false;
      quiet = false;
      onNumber = null;
      setOwner("self");
      suggest.hidden = true;
      suggestList.replaceChildren();
    },
    // An existing file, for editing: its values, and no suggestions (the server still checks the number rules).
    fill(p) {
      quiet = true;
      ownerTouched = true;
      field("full_name").value = p.full_name || "";
      field("phone").value = p.phone || "";
      const sex = box.querySelector(`[name="sex"][value="${p.sex}"]`);
      if (sex) sex.checked = true;
      setOwner(p.phone_owner || "self");
      const rel = box.querySelector(`[name="guardian_relation"][value="${p.guardian_relation}"]`);
      if (rel) rel.checked = true;
      field("age").value = p.birth_date_estimated && p.age != null ? p.age : "";
    },
    // What the finder held: a name, or digits as the mobile.
    prefill(q) {
      const digits = /^[\d\s+٠-٩۰-۹-]+$/.test(q);
      if (q && !digits && !field("full_name").value) { field("full_name").value = q; suggestNames(); }
      if (q && digits && !field("phone").value) { field("phone").value = q; checkNumber(); }
      (digits ? field("full_name") : field("phone")).focus();
    },
    values() {
      const out = { full_name: field("full_name").value.trim().replace(/\s+/g, " "), phone: phoneDigits(field("phone").value),
                    phone_owner: owner(), sex: checked("sex") };
      if (out.phone_owner === "guardian") out.guardian_relation = checked("guardian_relation");
      const age = parseWhole(field("age").value.trim());
      if (age !== null) out.age = age;
      return out;
    },
    // The first thing missing, in the screen's order, or "".
    problem(v) {
      if (v.full_name.split(" ").filter(Boolean).length < 3) return label("need_three");
      if (!EGYPT_MOBILE.test(v.phone)) return label("need_phone");
      if (v.phone_owner === "guardian" && !v.guardian_relation) return label("need_relation");
      if (!v.sex) return label("need_sex");
      if ((v.age == null && !v.birth_date) || v.age > 130) return label("need_age");
      return "";
    },
    // The server refused a duplicate (duplicate_patient, phone_taken): offer the file it named.
    refused(result) {
      const error = result && result.data && result.data.error;
      if (!error || !["duplicate_patient", "phone_taken"].includes(error.code)) return false;
      noticeText.textContent = error.message;
      noticeList.replaceChildren(...(error.patient ? [pickButton({ ...error.patient, file_no: error.patient.file_no }, label)] : []));
      newChild.hidden = true;
      notice.hidden = false;
      return true;
    },
  };
}
