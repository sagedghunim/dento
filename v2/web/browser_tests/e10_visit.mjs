// The doctor's visit screen (Saged, 2026-11-13), signed in as a doctor (HAMADA_BT_USER=ahmed) with a queue today.
// The doctor's "Call in" on Today opens /visits/<id>; the timer runs; the notes save as the doctor types, once per
// text even through a dropped connection and an expired session; a double-clicked procedure and prescription save
// once; the dental chart opens inside the screen; leaving warns; Today comes back to the visit; "Done with this
// visit" (double-clicked) finishes once and lands on Today with the next patient. At 390 px nothing scrolls sideways.
// The desk (HAMADA_BT_DESK, default sara) calling a patient stays on Today.
// With HAMADA_BT_SHOTS=<folder> it takes one 1280 px and one 390 px screenshot of the visit screen.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { browser, check, freeRoom, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const DESK = process.env.HAMADA_BT_DESK || "sara";

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const str = (key) => `document.querySelector('#strings [data-k="${key}"]').textContent`;
const savedState = `document.querySelector("[data-save-state]").textContent === ${str("note_saved")}`;

async function typeNotes(page, text) {
  await page.eval(`(() => { const b = document.querySelector("[data-notes]"); b.focus(); b.value = ${JSON.stringify(text)};
                            b.dispatchEvent(new Event("input", { bubbles: true })); })()`);
}

export async function run() {
  const { session, membership, api } = await signIn();
  const desk = await signIn(DESK);
  const config = (await api("GET", "/api/v1/clinic")).data;
  const service = config.services.find((s) => s.kind === "procedure").id;
  await freeRoom(api, membership);  // nobody of this doctor's may be in the room before the test
  const walkIn = async (name) => (await api("POST", "/api/v1/appointments", {
    doctor_id: membership, service_id: service, arrive: true, new_patient: { full_name: name, phone: `0101${String(Date.now()).slice(-7)}` },
  })).data.appointment;
  const first = await walkIn(`Bt Visit ${stamp}`);
  const second = await walkIn(`Bt Next ${stamp}`);
  const record = async (id) => (await api("GET", `/api/v1/appointments/${id}/record`)).data;

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/");
    await page.until(`Boolean(document.querySelector('.appt[data-id="${first.id}"] [data-act="call"]'))`, 8000, "the waiting row offers Call in");
    await page.eval(`document.querySelector('.appt[data-id="${first.id}"] [data-act="call"]').click()`);
    await page.until(`location.pathname === "/visits/${first.id}" && Boolean(document.querySelector("[data-notes]"))`, 15000, "the doctor's call-in opens the visit screen");
    check((await page.eval(`document.querySelector("#visit-patient").textContent`)).includes(first.patient.name), "the band names the patient");
    check(await page.eval(`Boolean(document.querySelector('a[href^="tel:"]'))`), "the phone is a tap-to-call link");
    const t1 = await page.eval(`document.querySelector("[data-timer]").textContent`);
    await sleep(2200);
    check((await page.eval(`document.querySelector("[data-timer]").textContent`)) !== t1, `the timer runs (${t1})`);

    // The notes save as the doctor types: one version per text.
    await typeNotes(page, "حشو 36، تنظيف");
    await page.until(savedState, 10000, "the notes are saved");
    let note = (await record(first.id)).note;
    check(note && note.notes === "حشو 36، تنظيف" && note.version === 1, "the server holds the text, version 1");

    // A dropped connection: the text waits and is saved once when it comes back.
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await typeNotes(page, "حشو 36، تنظيف، أشعة");
    await page.until(`document.querySelector("[data-save-state]").classList.contains("bad")`, 40000, "it says the notes aren't saved yet");
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`window.dispatchEvent(new Event("online"))`);
    await page.until(savedState, 20000, "saved once the connection is back");
    note = (await record(first.id)).note;
    check(note.notes === "حشو 36، تنظيف، أشعة" && note.version === 2, "saved once after the drop (version 2)");

    // A procedure without the chart, double-clicked: recorded once.
    const procsBefore = (await record(first.id)).procedures.length;
    const picked = await page.eval(`(() => { const s = document.querySelector('[data-add-procedure] select');
      const o = [...s.options].find((x) => x.value && x.dataset.unit !== "tooth") || s.options[1]; s.value = o.value;
      s.dispatchEvent(new Event("change", { bubbles: true }));
      if (o.dataset.unit === "tooth") document.querySelector('[data-add-procedure] [name="tooth"]').value = "36";
      const b = document.querySelector('[data-add-procedure] [data-submit]'); b.click(); b.click(); return o.textContent; })()`);
    await page.until(`document.querySelectorAll("[data-procedures] li").length === ${procsBefore + 1}`, 10000, `the procedure shows in the list (${picked})`);
    await sleep(500);
    check((await record(first.id)).procedures.length === procsBefore + 1, "a double click recorded one procedure");
    check(await page.eval(`document.querySelector("[data-no-procedures]").hidden`), "the empty note hides");

    // The dental chart opens inside the screen; a procedure recorded there joins the list.
    check(!(await page.eval(`document.querySelector("[data-chart-fold]").open`)), "the chart starts closed");
    await page.eval(`document.querySelector("[data-chart-fold] summary").click()`);
    await page.until(`document.querySelectorAll(".tooth").length === 52`, 10000, "the chart loads 52 teeth");
    await page.eval(`document.querySelector('.tooth[data-tooth="26"]').click()`);
    await page.until(`!document.querySelector("[data-panel]").hidden`, 3000, "the tooth panel opens");
    await page.eval(`(() => { const s = document.querySelector('[data-part="procedure"] select'); s.value = s.options[1].value;
                              s.dispatchEvent(new Event("change", { bubbles: true })); document.querySelector('[data-part="procedure"] [data-submit]').click(); })()`);
    await page.until(`document.querySelectorAll("[data-procedures] li").length === ${procsBefore + 2}`, 10000, "a chart procedure joins the visit's list");
    await page.eval(`document.querySelector("[data-panel-close]").click()`);
    await page.eval(`document.querySelector("[data-chart-fold]").open = false`);

    // A prescription, double-clicked: saved once.
    const rxBefore = (await record(first.id)).prescriptions.length;
    await page.eval(`document.querySelector("[data-open-rx]").click()`);
    await page.until(`document.getElementById("rx-dialog").open`, 3000, "the prescription dialog opens");
    await page.eval(`(() => { const f = document.querySelector("#rx-dialog form"); f.elements.medicine.value = "Bt Amoxicillin 500";
                              document.querySelector("[data-rx-add]").click(); })()`);
    check((await page.eval(`document.querySelectorAll("[data-rx-lines] li").length`)) === 1, "the medicine is a line");
    await page.eval(`(() => { const b = document.querySelector("#rx-dialog [data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!document.getElementById("rx-dialog").open`, 10000, "the dialog closes after saving");
    await sleep(500);
    check((await record(first.id)).prescriptions.length === rxBefore + 1, "a double click wrote one prescription");
    check((await page.eval(`document.querySelectorAll("[data-rx-list] li").length`)) === rxBefore + 1, "the visit lists it");

    await page.eval(`window.scrollTo(0, 0)`);
    await shot(page, "v2-visit-1280.png");

    // Leaving warns. The page needs a real tap first (browsers ask only after one).
    const box = await page.eval(`(() => { const r = document.querySelector("[data-notes]").getBoundingClientRect(); return [r.x + 20, r.y + 20]; })()`);
    await page.tap(box[0], box[1]);
    let asked = null;
    page.on((m) => { if (m.method === "Page.javascriptDialogOpening") asked = m.params.type; });
    page.cdp("Page.navigate", { url: (await page.eval("location.origin")) + "/patients" });
    for (let i = 0; i < 50 && !asked; i++) await sleep(100);
    check(asked === "beforeunload", `leaving asks first (${asked})`);
    await page.cdp("Page.handleJavaScriptDialog", { accept: false });
    await sleep(300);
    check((await page.eval("location.pathname")) === `/visits/${first.id}`, "staying keeps the visit open");

    // Today comes back to the visit (accept the leave warning on the way).
    page.on((m) => { if (m.method === "Page.javascriptDialogOpening") page.cdp("Page.handleJavaScriptDialog", { accept: true }); });
    await page.open("/");
    check((await page.eval("location.pathname")) === `/visits/${first.id}`, "Today goes back to the open visit");

    // An expired session: the typed text waits in the tab and is saved after signing in again.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: (await page.eval("location.origin")) });
    await typeNotes(page, "حشو 36، تنظيف، أشعة، متابعة بعد أسبوع");
    await page.until(`location.pathname === "/login"`, 15000, "an expired session goes to sign-in");
    await page.cookie(session);
    await page.open(`/visits/${first.id}`);
    await page.until(savedState, 15000, "the waiting text is saved after signing in");
    note = (await record(first.id)).note;
    check(note.notes === "حشو 36، تنظيف، أشعة، متابعة بعد أسبوع" && note.version === 3, "saved once (version 3)");

    // "Done with this visit", double-clicked: finished once, back on Today with the next patient.
    await page.eval(`(() => { const b = document.querySelector("[data-finish]"); b.click(); b.click(); })()`);
    await page.until(`location.pathname === "/" && Boolean(document.querySelector("[data-queue-card]"))`, 15000, "the finish goes back to Today");
    const visit = (await api("GET", `/api/v1/appointments/${first.id}`)).data.appointment;
    check(visit.status === "done", "the visit is done");
    await page.until(`Boolean(document.querySelector('.appt[data-id="${second.id}"] [data-act="call"]'))`, 8000, "the next waiting patient shows with Call in");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }

  // At 390 px: the next patient's visit, from a tap on Call in.
  const phone = await browser({ width: 390, height: 844, touch: true });
  try {
    await phone.cookie(session);
    await phone.open("/");
    await phone.eval(`document.querySelector('.appt[data-id="${second.id}"] [data-act="call"]').click()`);
    await phone.until(`location.pathname === "/visits/${second.id}" && Boolean(document.querySelector("[data-notes]"))`, 15000, "the call-in opens the visit at 390 px");
    check(await phone.eval(`document.documentElement.scrollWidth <= 390`), "the visit screen fits 390 px");
    const sizes = await phone.eval(`(() => { const f = document.querySelector("[data-finish]").getBoundingClientRect();
      const n = document.querySelector("[data-notes]").getBoundingClientRect();
      return { finish: f.height, notes: n.height, notesWidth: n.width, bottom: f.bottom }; })()`);
    check(sizes.finish >= 44 && sizes.notes >= 200 && sizes.notesWidth >= 300, `big targets: finish ${sizes.finish}px, notes ${sizes.notes}px high`);
    check(sizes.bottom <= 844, "the finish button stays on screen");
    await shot(phone, "v2-visit-390.png");
    check(phone.errors().length === 0, `no script errors (${phone.errors().join(" | ")})`);
  } finally {
    phone.close();
  }
  await api("POST", `/api/v1/appointments/${second.id}/finish`, { status: "in_room" });

  // The desk calling a patient in stays on Today.
  const third = await walkIn(`Bt Desk ${stamp}`);
  const deskPage = await browser({ width: 1280, height: 900 });
  try {
    await deskPage.cookie(desk.session);
    await deskPage.open("/");
    await deskPage.until(`Boolean(document.querySelector('.appt[data-id="${third.id}"] [data-act="call"]'))`, 8000, "the desk sees the row");
    await deskPage.eval(`document.querySelector('.appt[data-id="${third.id}"] [data-act="call"]').click()`);
    await deskPage.until(`document.querySelector('.appt[data-id="${third.id}"]') && document.querySelector('.appt[data-id="${third.id}"]').dataset.status === "in_room"`, 15000, "the row turns to in the room");
    check((await deskPage.eval("location.pathname")) === "/", "the desk stays on Today");
  } finally {
    deskPage.close();
  }
  await api("POST", `/api/v1/appointments/${third.id}/finish`, { status: "in_room" });
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
