// Follow-ups phase B (Saged, 2026-11-13), as a doctor with a queue today (HAMADA_BT_USER=ahmed); the desk is
// HAMADA_BT_DESK (default sara).
// A finished visit with two planned sessions shows on the desk's Today in "just out of the visit", by the polling
// alone. "Book" opens the Book dialog on the patient and the doctor, on session 1's suggested day, with the
// sessions' days marked; a double-clicked Book books session 1 once, linked to its follow-up, and the dialog moves
// straight to session 2; "Skip" closes it. The strip keeps the session left; "Later" (double-clicked) takes the row
// off the strip and Follow-ups keeps it. The plan goes to WhatsApp. At 390 px nothing scrolls sideways.
// With HAMADA_BT_SHOTS=<folder> it takes the dialog at 1280 px and Today's strip at 390 px. Not in run.mjs.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { BASE, browser, check, freeRoom, inArabic, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const DESK = process.env.HAMADA_BT_DESK || "sara";
const D = `document.getElementById("book-dialog")`;

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const html = async (who, path) => (await fetch(BASE + path, { headers: { Cookie: `hamada_session=${who.session}` }, redirect: "manual" }));

// The screens are read in Arabic: the doctor and the desk run in Arabic, and get their own language back after.
export async function run() {
  const backs = [await inArabic((await signIn()).api), await inArabic((await signIn(DESK)).api)];
  try {
    await runInArabic();
  } finally {
    for (const back of backs) await back();
  }
}

async function runInArabic() {
  const doctor = await signIn();
  const desk = await signIn(DESK);
  const name = `Bt Out ${stamp}`;
  const phoneNo = `0101${String(Date.now()).slice(-7)}`;
  const config = (await doctor.api("GET", "/api/v1/clinic")).data;
  const service = config.services.find((s) => s.kind === "procedure").id;
  await freeRoom(doctor.api, doctor.membership);
  const made = await doctor.api("POST", "/api/v1/appointments/walk-in", { doctor_id: doctor.membership, service_id: service,
                                                                  new_patient: { full_name: name, phone: phoneNo, sex: "m", age: 30 } });
  check(made.status === 201, `a walk-in for the doctor (${made.status} ${JSON.stringify(made.data).slice(0, 200)})`);
  const visit = made.data.appointment;
  check((await doctor.api("POST", `/api/v1/appointments/${visit.id}/call`, { status: "waiting" })).status === 200, "called in");
  const added = await doctor.api("POST", `/api/v1/appointments/${visit.id}/follow-ups`,
    { items: [{ label: "جلسة تانية", days: 7 }, { label: "جلسة تالتة", days: 14 }] });
  check(added.status === 201, "two sessions planned in the visit");
  const items = added.data.upcoming.filter((u) => u.visit_id === visit.id);
  const row = `[...document.querySelectorAll('[data-slot="followups"] [data-fu]')].find((r) => r.dataset.name === ${JSON.stringify(name)})`;
  const listed = async () => { const text = await (await html(desk, "/follow-ups")).text(); return items.filter((i) => text.includes(`data-item="${i.id}"`)).length; };

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(desk.session);
    await page.open("/");
    await page.until(`Boolean(document.querySelector("[data-queue-card]"))`, 10000, "Today opens for the desk");
    check(!(await page.eval(`Boolean(${row})`)), "not in the strip while the visit is open");

    // The doctor finishes: the strip shows the patient by the polling alone.
    check((await doctor.api("POST", `/api/v1/appointments/${visit.id}/finish`, { status: "in_room" })).status === 200, "the doctor finishes");
    await page.until(`Boolean(${row})`, 20000, "the patient appears in the strip without a reload");
    check((await page.eval(`document.querySelector("[data-just-out] h2").textContent`)).trim() === "خارج من الكشف دلوقتي", "the strip's title");
    check((await page.eval(`${row}.querySelectorAll("[data-item]").length`)) === 2, "with its two sessions");
    check(await page.eval(`Boolean(${row}.querySelector('a[href="/follow-ups/whatsapp/${visit.patient.id}"]'))`), "and the plan by WhatsApp");

    // "Book": the dialog on the patient and doctor, session 1 of 2, on its suggested day, the days marked.
    await page.eval(`${row}.querySelector("[data-fu-book]").click()`);
    await page.until(`${D}.open && !${D}.querySelector("[data-plan-bar]").hidden`, 5000, "Book opens the dialog on the plan");
    check((await page.eval(`${D}.querySelector("[data-picked-name]").textContent`)).includes(name), "on the patient");
    check((await page.eval(`(${D}.querySelector('[name="doctor"]:checked') || {}).value`)) === String(doctor.membership), "and the doctor");
    check((await page.eval(`${D}.querySelector("[data-plan-step]").textContent`)).includes("جلسة تانية"), "it names session 1");
    check(await page.eval(`${D}.querySelector("[data-change-patient]").hidden`), "the patient can't be swapped inside a plan");
    await page.until(`${D}.querySelectorAll(".cal-day.suggested").length >= 1`, 8000, "the suggested days are marked on the calendar");
    check(await page.eval(`Boolean(${D}.querySelector('.cal-day.suggested.now[data-day="${items[0].due_on}"]'))`), "session 1's day carries the strong mark");
    const pickTime = `(() => { const i = [...${D}.querySelectorAll('[data-slots] input:not(:disabled)')][0]; if (i && !i.checked) { i.checked = true; i.dispatchEvent(new Event("change", { bubbles: true })); } })()`;
    await sleep(1200);  // the doctor's month and the day's times
    await page.eval(pickTime);
    await shot(page, "v2-plan-book-1280.png");

    // A double-clicked Book: one booking, linked, and the dialog is on session 2 without booking it.
    await page.eval(`(() => { const b = ${D}.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`${D}.open && ${D}.querySelector("[data-plan-step]").textContent.includes("جلسة تالتة")`, 15000, "the dialog moves straight to session 2");
    await page.eval(`${D}.querySelector("[data-submit]").click()`);  // the tail of a triple tap: ignored
    await sleep(1500);
    check((await listed()) === 1, "session 1 is booked once and session 2 is not");

    // "Skip" on the last session closes the dialog; the strip keeps what is left.
    await page.eval(`${D}.querySelector("[data-plan-skip]").click()`);
    await page.until(`!${D}.open`, 5000, "Skip on the last session closes the dialog");
    await page.until(`Boolean(${row}) && ${row}.querySelectorAll("[data-item]").length === 1`, 20000, "the strip keeps the session left");

    // The plan by WhatsApp: on to wa.me with the patient's number and the text.
    const wa = await html(desk, `/follow-ups/whatsapp/${visit.patient.id}`);
    const target = wa.headers.get("location") || "";
    check(wa.status === 302 && target.startsWith(`https://wa.me/2${phoneNo}?text=`), `WhatsApp opens on the patient (${wa.status} ${target.slice(0, 60)})`);
    check(decodeURIComponent(target).includes("جلسة تانية") && decodeURIComponent(target).includes("جلسة تالتة"), "with both sessions");

    // At 390 px, with the strip showing.
    const phone = await browser({ width: 390, height: 844, touch: true });
    try {
      await phone.cookie(desk.session);
      await phone.open("/");
      await phone.until(`Boolean(document.querySelector("[data-queue-card]"))`, 10000, "Today at 390 px");
      await phone.until(`Boolean(${row})`, 10000, "the strip is on Today at 390 px");
      check(await phone.eval(`document.documentElement.scrollWidth <= 390`), "Today fits 390 px");
      const small = await phone.eval(`[...document.querySelectorAll("[data-fu] button, [data-fu] a.btn")].map((b) => b.getBoundingClientRect().height).filter((h) => h < 44).length`);
      check(small === 0, "every tap target of the strip is at least 44 px high");
      await phone.eval(`window.scrollTo(0, document.querySelector("[data-just-out]").getBoundingClientRect().top + window.scrollY - 70)`);
      await shot(phone, "v2-just-out-390.png");
      check(phone.errors().length === 0, `no script errors (${phone.errors().join(" | ")})`);
    } finally {
      phone.close();
    }

    // "Later", double-clicked: off the strip, still in Follow-ups.
    await page.eval(`(() => { const b = ${row}.querySelector("[data-fu-later]"); b.click(); b.click(); })()`);
    await page.until(`!(${row}) || ${row}.hidden`, 10000, "Later takes the row off the strip");
    await sleep(800);
    check((await listed()) === 1, "Follow-ups keeps it");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }

}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
