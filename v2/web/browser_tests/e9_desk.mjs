// The front desk review (docs/v2/reviews/desk-workflow.md), as the desk (sara): a ready row offers "Call in" and
// never says "didn't come"; who needs the desk comes first and finished, paid rows fold; phones call and open
// WhatsApp; "Didn't come" by hand, double-clicked, saves once and its toast undoes it; it waits while the
// connection is down; "Book the next visit" opens Book on the patient; Book starts with "here now" and "collect"
// unticked and Walk-in takes the typed name with "collect" ticked; the earlier balance is collected with the
// finish, once on a double click; 390 px without sideways scrolling; an expired session goes to sign-in.
// HAMADA_BT_SHOTS=<folder> takes Today at 1280 px and at 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { BASE, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const RANK = { in_room: 0, ready: 1, with_assistant: 2, waiting: 3, done: 4, booked: 5, no_show: 6, cancelled: 7 };

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const rowOf = (id) => `document.querySelector('.appt[data-id="${id}"]')`;
const toastText = `document.querySelector("#toast.show [data-text]") ? document.querySelector("#toast [data-text]").textContent : ""`;

export async function run() {
  const { session, api } = await signIn();
  const config = (await api("GET", "/api/v1/clinic")).data;
  const doctor = config.doctors.find((d) => d.today.some((s) => s.mode === "queue"));
  if (!doctor) throw new Error("This test needs a queue doctor working today.");
  const service = config.services.find((s) => s.kind === "consultation").id;
  const phone = `0101${String(Date.now()).slice(-7)}`;
  const book = async (name, body = {}) => (await api("POST", "/api/v1/appointments", {
    doctor_id: doctor.id, service_id: service, new_patient: { full_name: name, phone }, ...body })).data.appointment;
  const ready = await book(`Bt Ready ${stamp}`, { arrive: true });
  await api("POST", `/api/v1/appointments/${ready.id}/ready`, { status: "waiting" });
  const late = await book(`Bt Late ${stamp}`);
  const late2 = await book(`Bt Late2 ${stamp}`);
  const status = async (id) => (await api("GET", `/api/v1/appointments/${id}`)).data.appointment.status;

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/");
    await page.eval(`(() => { const all = document.querySelector('[data-filter-doctor=""]'); if (all) all.click(); })()`);
    await page.until(`${rowOf(ready.id)}?.dataset.status === "ready"`, 15000, "the ready row shows");

    // 1. Every status its pill and its step.
    const readyRow = await page.eval(`(() => { const r = ${rowOf(ready.id)}; return { pill: r.querySelector(".appt-side .pill").textContent.trim(),
      red: Boolean(r.querySelector(".appt-side .pill.red")), call: Boolean(r.querySelector('[data-act="call"]')) }; })()`);
    check(!readyRow.red && readyRow.pill.length > 0 && readyRow.call, `a ready row says "${readyRow.pill}" and offers Call in`);
    check(await page.eval(`[...document.querySelectorAll('.appt[data-status="ready"], .appt[data-status="with_assistant"]')].every((r) => !r.querySelector(".appt-side .pill.red"))`),
      "no ready or with-the-assistant row is marked red");

    // 5. Order and the fold.
    const order = await page.eval(`[...document.querySelectorAll(".queue-group")].map((g) => ({
      open: [...g.querySelectorAll('[data-rows="open"] > .appt')].map((r) => r.dataset.status + ":" + r.dataset.due),
      done: [...g.querySelectorAll('[data-rows="done"] > .appt')].map((r) => r.dataset.status + ":" + r.dataset.due),
      folded: !g.querySelector("[data-fold]").open, count: g.querySelector("[data-fold-count]").textContent }))`);
    for (const g of order) {
      const ranks = g.open.map((s) => RANK[s.split(":")[0]]);
      check(ranks.every((r, i) => i === 0 || ranks[i - 1] <= r), `a doctor's open rows go in, ready, waiting, owing, booked (${g.open.length} rows)`);
      check(g.done.every((s) => s === "done:0") && !g.open.includes("done:0"), "finished and paid rows are folded, owing ones stay out");
      check(g.folded, `the fold starts closed (${g.done.length} inside)`);
    }
    const firstWaiting = await page.eval(`(() => { const r = document.querySelector('[data-rows="open"] > .appt'); return r ? Math.round(r.getBoundingClientRect().top) : 0; })()`);
    console.log(`  info: the first row of the list starts at y = ${firstWaiting}`);
    await page.eval(`document.querySelector(".queue-group [data-fold]:not([hidden]) > summary")?.click()`);
    check(await page.eval(`!document.querySelector(".queue-group [data-fold]:not([hidden])") || document.querySelector(".queue-group [data-fold]:not([hidden])").open`),
      "one tap opens the finished rows");
    const summaryHeight = await page.eval(`(() => { const s = document.querySelector(".queue-group [data-fold]:not([hidden]) > summary"); return s ? s.getBoundingClientRect().height : 44; })()`);
    check(summaryHeight >= 44, `the fold's tap target is ${summaryHeight} px tall`);

    // 7. Phones.
    const links = await page.eval(`(() => { const r = ${rowOf(ready.id)}; const tel = r.querySelector('a[href^="tel:"]');
      const wa = r.querySelector('a[href^="https://wa.me/"]'); return { tel: tel && tel.getAttribute("href"), wa: wa && wa.getAttribute("href"),
      h: tel ? tel.getBoundingClientRect().height : 0 }; })()`);
    check(links.tel === `tel:${phone}` && links.wa === `https://wa.me/20${phone.slice(1)}`, `the phone calls and opens WhatsApp (${links.tel}, ${links.wa})`);
    check(links.h >= 40, `the phone link is ${links.h} px tall to the finger`);

    await shot(page, "v2-today-desk-1280.png");

    // 6. "Didn't come", double-clicked: one save, then the toast's Undo.
    await page.until(`${rowOf(late.id)}?.dataset.status === "booked"`, 15000, "the booked row shows");
    await sleep(800);  // past the tap guard
    await page.eval(`${rowOf(late.id)}.querySelector("details.menu > summary").click()`);
    check(await page.eval(`!${rowOf(late.id)}.querySelector('[data-act="no-show"]').hidden`), "a queue booking can be marked didn't come");
    await page.eval(`(() => { const b = ${rowOf(late.id)}.querySelector('[data-act="no-show"]'); b.click(); b.click(); })()`);
    await page.until(`${rowOf(late.id)}?.dataset.status === "no_show"`, 15000, "the row says didn't come");
    check((await status(late.id)) === "no_show", "the server has it as a no-show");
    const toast = await page.eval(toastText);
    check(toast.includes(`Bt Late ${stamp}`), `the toast names the patient: "${toast}"`);
    await page.eval(`document.querySelector("#toast [data-toast-action]").click()`);
    await page.until(`${rowOf(late.id)}?.dataset.status === "booked"`, 15000, "Undo puts it back to booked");
    check((await status(late.id)) === "booked", "the server has it booked again");

    // A dropped connection: the row shows it is saving, and the save goes through once the line is back.
    await page.until(`${rowOf(late2.id)}?.dataset.status === "booked"`, 15000, "the second booked row shows");
    await sleep(800);
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`${rowOf(late2.id)}.querySelector("details.menu > summary").click()`);
    await page.eval(`${rowOf(late2.id)}.querySelector('[data-act="no-show"]').click()`);
    await sleep(1500);
    check(await page.eval(`${rowOf(late2.id)}.classList.contains("saving")`), "while offline the row shows it is still saving");
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.until(`${rowOf(late2.id)}?.dataset.status === "no_show"`, 30000, "the save goes through once the line is back");
    check((await status(late2.id)) === "no_show", "the server has it once");

    // 4. Next visit from the row menu: Book opens on the patient, with no search.
    await sleep(800);
    await page.eval(`${rowOf(ready.id)}.querySelector("details.menu > summary").click()`);
    await page.eval(`${rowOf(ready.id)}.querySelector('[data-act="next"]').click()`);
    await page.until(`document.getElementById("book-dialog").open`, 3000, "Book opens");
    const next = await page.eval(`({ name: document.querySelector("#book-dialog [data-picked-name]").textContent,
      finder: document.querySelector("#book-dialog [data-finder]").hidden,
      doctor: document.querySelector('#book-dialog [name="doctor"]:checked')?.value })`);
    check(next.name === `Bt Ready ${stamp}` && next.finder, "the patient is already picked");
    check(next.doctor === String(doctor.id), "with the same doctor");
    await page.eval(`document.querySelector("#book-dialog [data-close]").click()`);

    // 2. Book starts unticked; Walk-in takes the typed name and collects.
    await page.eval(`document.querySelector("[data-open-book]").click()`);
    await page.until(`document.getElementById("book-dialog").open`, 3000, "Book opens empty");
    const ticks = await page.eval(`({ arrive: document.querySelector('#book-dialog [name="arrive"]')?.checked, collect: document.querySelector('#book-dialog [name="collect"]')?.checked })`);
    check(ticks.arrive === false && ticks.collect === false, "a booking starts with 'here now' and 'collect' unticked");
    await page.eval(`document.querySelector("#book-dialog [data-close]").click()`);
    await page.eval(`(() => { const f = document.querySelector("[data-quick-book]"); f.elements.q.value = "Bt Ready ${stamp}"; f.querySelector("[data-open-walkin]").click(); })()`);
    await page.until(`document.getElementById("walkin-dialog").open`, 3000, "Walk-in opens");
    const walk = await page.eval(`({ q: document.querySelector('#walkin-dialog [name="q"]').value, collect: document.querySelector('#walkin-dialog [name="collect"]').checked })`);
    check(walk.q === `Bt Ready ${stamp}` && walk.collect, "Walk-in has the typed name, and 'collect' is ticked");
    await page.until(`document.querySelectorAll("#walkin-dialog [data-pick-patient]").length > 0`, 8000, "and searches it");
    await page.eval(`document.querySelector("#walkin-dialog [data-close]").click()`);

    // 3. The earlier balance, collected with the finish, once on a double click.
    const owing = await page.eval(`(() => { const r = [...document.querySelectorAll(".appt")].find((x) => Number(x.dataset.earlier) > 0);
      return r ? { patient: Number(r.dataset.patient), earlier: Number(r.dataset.earlier), name: r.dataset.name } : null; })()`);
    if (!owing) {
      console.log("  skipped: no patient on Today owes from before");
    } else {
      const before = (await api("GET", `/api/v1/patients/${owing.patient}/balance`)).data.balance.owed;
      const visit = (await api("POST", "/api/v1/appointments/walk-in", { patient_id: owing.patient, doctor_id: doctor.id })).data.appointment;
      await api("POST", `/api/v1/appointments/${visit.id}/call`, { status: "waiting" });
      await page.until(`${rowOf(visit.id)}?.dataset.status === "in_room"`, 15000, "the owing patient's visit is with the doctor");
      check(Number(await page.eval(`${rowOf(visit.id)}.dataset.earlier`)) === owing.earlier, "the row knows the earlier balance");
      check(await page.eval(`Boolean(${rowOf(visit.id)}.querySelector(".earlier"))`), "and shows it");
      await sleep(800);
      await page.eval(`${rowOf(visit.id)}.querySelector('[data-act="finish"]').click()`);
      await page.until(`document.getElementById("finish-dialog").open`, 3000, "the finish dialog opens");
      const box = await page.eval(`({ shown: !document.querySelector("#finish-dialog [data-earlier]").hidden,
        ticked: document.querySelector('#finish-dialog [name="with_earlier"]').checked,
        amount: document.querySelector('#finish-dialog [name="earlier_amount"]').value })`);
      check(box.shown && box.ticked && box.amount.length > 0, `"also collect what is owed from before" is ticked with ${box.amount}`);
      await page.eval(`(() => { const b = document.querySelector("#finish-dialog [data-submit]"); b.click(); b.click(); })()`);
      await page.until(`!document.getElementById("finish-dialog").open`, 15000, "the dialog closes after the finish");
      const after = (await api("GET", `/api/v1/patients/${owing.patient}/balance`)).data.balance.owed;
      check(after === before - owing.earlier, `the earlier balance was collected once (${before} -> ${after})`);
      check((await status(visit.id)) === "done", "the visit is done");
    }

    // Phones: 390 px, no sideways scrolling; the one phone screenshot.
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await page.open("/");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "Today fits 390 px");
    await shot(page, "v2-today-desk-390.png");

    // An expired session: a row action says signed out, and a page load goes to sign-in.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: BASE });
    await page.open("/");
    check((await page.eval("location.pathname")) === "/login", "an expired session goes to sign-in");
    check(page.errors().length === 0, `no script errors ${page.errors().join(" | ")}`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e9_desk against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
