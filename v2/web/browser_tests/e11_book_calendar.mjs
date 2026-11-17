// Booking with a calendar and times (Saged, 2026-11-13), as the desk (sara): the Book dialog opens on today's
// month and moves across months and into next year; with a doctor picked, their days off are greyed and working
// days hint at free times; a day shows the doctor's times, taken ones marked; a double-clicked Book books once;
// the booking lands in the day's "booked" list with its time, never among the waiting, and its row says "booked";
// a dropped connection keeps the booking for "Try again", which books once; an expired session says signed out;
// at 390 px the calendar fits and the times wrap; every button of Today, Book and Walk-in answers.
// HAMADA_BT_SHOTS=<folder> takes the open dialog at 1280 and 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { BASE, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const NET = { latency: 0, downloadThroughput: -1, uploadThroughput: -1 };

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const D = `document.getElementById("book-dialog")`;
const toastText = `(document.querySelector("#toast [data-text]") || {}).textContent || ""`;

async function openBook(page, doctorId) {
  await page.eval(`document.querySelector("[data-open-book]").click()`);
  await page.until(`${D}.open`, 5000, "Book opens");
  await page.eval(`${D}.querySelector("[data-new-patient]").click()`);
  if (doctorId) {
    await page.eval(`(() => { const i = ${D}.querySelector('[name="doctor"][value="${doctorId}"]'); i.checked = true; i.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    await page.until(`${D}.querySelector(".cal-day .cal-hint")?.textContent || ${D}.querySelector(".cal-day.off")`, 8000, "the month's hints load");
  }
}

// A new patient's must-haves (Saged, 2026-11-17): a three-part name, a mobile of their own, the sex and an age.
let phones = 0;
async function fill(page, name) {
  const phone = `015${String(Date.now()).slice(-6)}${String(++phones % 100).padStart(2, "0")}`;
  await page.eval(`(() => { const f = ${D}.querySelector('[name="full_name"]'); f.value = ${JSON.stringify(name)}; f.dispatchEvent(new Event("input", { bubbles: true }));
    ${D}.querySelector('[name="phone"]').value = "${phone}";
    ${D}.querySelector('[name="sex"][value="f"]').checked = true;
    ${D}.querySelector('[name="age"]').value = "30"; })()`);
}

// The first working day after today in the month shown (or the next month), then its first free time.
async function pickFutureTime(page) {
  for (let i = 0; i < 2; i++) {
    const day = await page.eval(`(() => { const b = [...${D}.querySelectorAll(".cal-day:not(:disabled)")].find((x) => x.dataset.day > ${D}.querySelector("[data-cal]").dataset.today && !x.classList.contains("full")); if (b) b.click(); return b ? b.dataset.day : ""; })()`);
    if (day) {
      await page.until(`${D}.querySelector('[data-slots] input')`, 8000, "the day's times show");
      const time = await page.eval(`(() => { const i = [...${D}.querySelectorAll('[data-slots] input:not(:disabled)')].find((x) => x.value); if (i) { i.checked = true; i.dispatchEvent(new Event("change", { bubbles: true })); } return i ? i.value : ""; })()`);
      if (time) return { day, time };
    }
    await page.eval(`${D}.querySelector('[data-cal-move="1"]').click()`);
    await sleep(800);
  }
  throw new Error("no free future time for this doctor");
}

export async function run() {
  const { session, api } = await signIn();
  const config = (await api("GET", "/api/v1/clinic")).data;
  const doctor = config.doctors.find((d) => d.today.some((s) => s.mode === "queue")) || config.doctors.find((d) => d.today.length) || config.doctors[0];
  if (!doctor) throw new Error("This test needs a doctor.");
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/");

    // 1. The calendar: today's month, Saturday first, free movement across the year's end, back to today.
    await openBook(page, null);
    const cal = await page.eval(`(() => { const c = ${D}.querySelector("[data-cal]"); return { title: c.querySelector("[data-cal-title]").textContent,
      heads: c.querySelectorAll(".cal-wd").length, today: c.querySelector('.cal-day[aria-current="date"]')?.dataset.day, picked: c.querySelector('.cal-day[aria-pressed="true"]')?.dataset.day,
      prev: c.querySelector('[data-cal-move="-1"]').disabled, past: [...c.querySelectorAll(".cal-day")].filter((b) => b.dataset.day < c.dataset.today).every((b) => b.disabled) }; })()`);
    check(cal.heads === 7 && cal.today && cal.today === cal.picked && cal.prev && cal.past, `opens on ${cal.title}, today picked, past days and the previous month disabled`);
    const titles = [];
    for (let i = 0; i < 3; i++) {
      await page.eval(`${D}.querySelector('[data-cal-move="1"]').click()`);
      titles.push(await page.eval(`${D}.querySelector("[data-cal-title]").textContent`));
    }
    check(new Set(titles).size === 3 && titles[2] !== cal.title, `next, next, next: ${titles.join(" → ")}`);
    await page.eval(`${D}.querySelector("[data-cal-today]").click()`);
    check(await page.eval(`${D}.querySelector("[data-cal-title]").textContent`) === cal.title, "Today goes back to this month");
    await page.eval(`${D}.querySelector("[data-close]").click()`);

    // 2. A doctor's days: off days greyed and struck, working days hinted; a future day's times; a double-click books once.
    await openBook(page, doctor.id);
    const days = await page.eval(`(() => { const all = [...${D}.querySelectorAll(".cal-day")];
      return { off: all.filter((b) => b.classList.contains("off")).every((b) => b.disabled && getComputedStyle(b.querySelector(".cal-num")).textDecorationLine.includes("line-through")),
               offCount: all.filter((b) => b.classList.contains("off")).length, hints: all.filter((b) => b.querySelector(".cal-hint").textContent).length }; })()`);
    check(days.off && days.hints > 0, `days off greyed and struck (${days.offCount}), working days hinted (${days.hints})`);
    const name = `Bt Cal ${stamp}`;
    await fill(page, name);
    const { day, time } = await pickFutureTime(page);
    const taken = await page.eval(`[...${D}.querySelectorAll('[data-slots] input[data-taken]')].every((i) => i.disabled)`);
    check(taken, "taken times can't be picked (only as an extra booking)");
    await shot(page, "book-calendar-1280.png");
    await page.eval(`(() => { const b = ${D}.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!${D}.open`, 10000, "the dialog closes after booking");
    const said = await page.eval(toastText);
    check(said.includes(name) && /\d|[٠-٩]/.test(said), `the toast says the day and time: "${said}"`);
    const theDay = (await api("GET", `/api/v1/today?day=${day}`)).data;
    const mine = theDay.queues.flatMap((q) => q.appointments).filter((a) => a.patient.name === name);
    check(mine.length === 1 && mine[0].starts_at === time && mine[0].status === "booked", "a double-clicked Book booked once, at the time picked");

    // 3. On that day's screen it is "booked", in its own list, by time, with no turn number.
    await page.open(`/?day=${day}`);
    await page.eval(`document.querySelector('[data-filter-doctor=""]')?.click()`);
    const row = await page.eval(`(() => { const r = document.querySelector('.appt[data-id="${mine[0].id}"]'); return r && { list: r.closest("[data-rows]").dataset.rows,
      time: r.querySelector(".appt-time").textContent, turn: Boolean(r.querySelector(".appt-time small")) }; })()`);
    check(row && row.list === "booked" && !row.turn, `the booking is in the booked list at ${row && row.time}, no turn shown`);
    check(await page.eval(`[...document.querySelectorAll('[data-rows="booked"] > .appt')].every((r) => r.dataset.status === "booked")
      && [...document.querySelectorAll('[data-rows="open"] > .appt')].every((r) => r.dataset.status !== "booked")`), "no booked row among the waiting");

    // 4. A dropped connection keeps the booking for "Try again", which books once.
    await page.open("/");
    await openBook(page, doctor.id);
    const offName = `Bt CalOff ${stamp}`;
    await fill(page, offName);
    const second = await pickFutureTime(page);
    await page.cdp("Network.emulateNetworkConditions", { offline: true, ...NET });
    await page.eval(`${D}.querySelector("[data-submit]").click()`);
    await page.until(`${D}.dataset.unknown === "1"`, 45000, "the outcome is unknown while offline");
    check(await page.eval(`${D}.open && ${D}.querySelector("[data-close]").hidden`), "the dialog stays open with no Close while unknown");
    await page.cdp("Network.emulateNetworkConditions", { offline: false, ...NET });
    await page.eval(`${D}.querySelector("[data-submit]").click()`);
    await page.until(`!${D}.open`, 15000, "Try again books");
    const after = (await api("GET", `/api/v1/today?day=${second.day}`)).data.queues.flatMap((q) => q.appointments).filter((a) => a.patient.name === offName);
    check(after.length === 1, "Try again booked once");

    // 5. The feed: a booking for today says "booked" with its time, never "arrived".
    const businessDay = (await api("GET", "/api/v1/today")).data.business_day;
    const todayFree = (await api("GET", `/api/v1/doctors/${doctor.id}/slots?day=${businessDay}`)).data.slots.find((s) => !s.taken && !s.passed);
    if (todayFree) {
      const feedName = `Bt Feed ${stamp}`;
      await api("POST", "/api/v1/appointments", { doctor_id: doctor.id, service_id: config.services.find((s) => s.kind === "consultation").id,
        new_patient: { full_name: feedName, phone: `015${String(Date.now() * 1000 + Math.floor(Math.random() * 1000)).slice(-8)}`, sex: "m", age: 30 }, starts_at: todayFree.starts_at });
      await page.open("/");
      const status = await page.eval(`[...document.querySelectorAll(".appt")].find((r) => (r.dataset.name || "").includes(${JSON.stringify(feedName)}))?.dataset.status || ""`);
      check(status === "booked", `the new booking's row says booked: "${status}"`);
    } else {
      console.log("  info: no free time left today; the feed check is skipped");
    }

    // 6. Every button of Today, Book and Walk-in answers (opens, toggles or closes), with no script error.
    await page.open("/");
    const before = page.errors().length;
    await page.eval(`document.querySelectorAll("[data-filter-doctor], [data-filter-section]").forEach((b) => b.click())`);
    await page.eval(`document.querySelector('[data-filter-doctor=""]')?.click()`);
    await page.eval(`document.querySelector("[data-open-walkin]")?.click()`);
    const walkin = await page.eval(`document.getElementById("walkin-dialog")?.open`);
    await page.eval(`(() => { const w = document.getElementById("walkin-dialog"); w.querySelector("[data-new-patient]")?.click(); w.querySelector("[data-back-to-search]")?.click(); w.querySelector("[data-close]").click(); })()`);
    await openBook(page, doctor.id);
    await page.eval(`(() => { const d = ${D}; d.querySelector("[data-back-to-search]")?.click(); d.querySelector("[data-new-patient]")?.click();
      d.querySelector('[data-cal-move="1"]').click(); d.querySelector('[data-cal-move="-1"]').click(); d.querySelector("[data-cal-today]").click(); })()`);
    await page.eval(`${D}.querySelector("[data-submit]").click()`);
    const empty = await page.eval(`${D}.querySelector("[data-error]").textContent`);
    check(walkin && empty.length > 0 && page.errors().length === before, `Walk-in opens and closes; Book with no patient says "${empty}"; no script errors`);

    // 7. 390 px: the calendar fits, the times wrap, every day is a 44 px target.
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await sleep(400);
    await fill(page, `Bt Narrow ${stamp}`);
    await pickFutureTime(page);
    const narrow = await page.eval(`(() => { const g = ${D}.querySelector("[data-cal-grid]"); const s = ${D}.querySelector("[data-slots]"); const days = [...g.querySelectorAll(".cal-day")];
      return { fits: g.scrollWidth <= g.clientWidth + 1 && ${D}.scrollWidth <= ${D}.clientWidth + 1, min: Math.min(...days.map((b) => b.getBoundingClientRect().height)),
               minW: Math.min(...days.map((b) => b.getBoundingClientRect().width)), wrap: s.scrollWidth <= s.clientWidth + 1, page: document.documentElement.scrollWidth <= 391 }; })()`);
    check(narrow.fits && narrow.wrap && narrow.page && narrow.min >= 44, `390 px: no sideways scroll, days ${Math.round(narrow.minW)}×${Math.round(narrow.min)} px, times wrap`);
    await page.eval(`${D}.querySelector("[data-cal]").scrollIntoView()`);
    await shot(page, "book-calendar-390.png");

    // 8. An expired session: Book says signed out instead of failing silently.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: BASE });
    await page.eval(`${D}.querySelector("[data-submit]").click()`);
    // The shell may send the page to sign-in at once; otherwise the dialog says signed out.
    await page.until(`location.pathname === "/login" || (${D} && ${D}.querySelector("[data-error]").textContent.length > 0 && !${D}.dataset.busy)`,
      15000, "an answer after the session ended");
    const out = await page.eval(`location.pathname === "/login" ? "sent to sign-in" : ${D}.querySelector("[data-error]").textContent`);
    check(out.length > 0, `an expired session: "${out}"`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED")).catch((e) => { console.error(e.message); process.exit(1); });
}
