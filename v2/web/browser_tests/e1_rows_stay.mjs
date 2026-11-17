// M1 decision R8: rows don't move under a resting finger, and a tap that lands just after a row changed
// is ignored (the row flashes), at phone width with touch.
//   1. The reviewer's E1: with every doctor shown, a finger rests on the second doctor's first row. Another
//      screen books a walk-in for the first doctor, above it. After the poll, the row is still under the finger.
//   2. Tap "Arrived" on a row; when it redraws, the same spot is now "Call in". A second tap right then is
//      ignored; one after 700 ms works.
import { BASE, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);

async function freeSlot(api, doctorId, day) {
  const slots = (await api("GET", `/api/v1/doctors/${doctorId}/slots?day=${day}`)).data;
  return slots.slots.find((s) => !s.taken);
}

export async function run() {
  const { session, api } = await signIn();
  const config = (await api("GET", "/api/v1/clinic")).data;
  const day = config.clinic.business_day;
  const working = config.doctors.filter((d) => d.today.length);
  if (working.length < 2) throw new Error("This test needs two doctors working today (the demo dental clinic, Saturday to Thursday).");
  const [first, second] = working;
  const service = config.services.find((s) => s.kind === "consultation").id;
  async function book(doctor, name, extra = {}) {
    const body = { doctor_id: doctor.id, service_id: service, new_patient: { full_name: name, phone: `015${String(Date.now() * 1000 + Math.floor(Math.random() * 1000)).slice(-8)}`, sex: "m", age: 30 }, ...extra };
    if (doctor.today.every((s) => s.mode === "slots")) body.starts_at = (await freeSlot(api, doctor.id, day)).starts_at;
    const r = await api("POST", "/api/v1/appointments", body);
    if (r.status !== 201) throw new Error(`booking failed: ${r.status} ${JSON.stringify(r.data)}`);
    return r.data.appointment;
  }
  await book(first, `Bt Above ${stamp}`);
  const rested = await book(second, `Bt Rested ${stamp}`);
  const tapped = await book(second, `Bt Tapped ${stamp}`);

  const page = await browser({ width: 390, height: 844, touch: true });
  try {
    await page.cookie(session);
    await page.open("/");
    await page.eval(`(() => { localStorage.clear(); const all = document.querySelector('[data-filter-doctor=""]'); if (all) all.click(); })()`);
    await sleep(300);

    // --- 1: the finger rests on the second doctor's row while a booking lands above it ---
    const rowAt = (id) => `(() => { const r = document.querySelector('.appt[data-id="${id}"]'); return r ? r.getBoundingClientRect().top : null; })()`;
    await page.eval(`(() => { const r = document.querySelector('.appt[data-id="${rested.id}"]'); window.scrollBy(0, r.getBoundingClientRect().top - 400); })()`);
    await sleep(300);
    const before = await page.eval(rowAt(rested.id));
    // The finger touched the screen there a moment ago and now rests (no move, no tap).
    await page.cdp("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 200, y: before + 10 }] });
    await page.cdp("Input.dispatchTouchEvent", { type: "touchCancel", touchPoints: [] });
    await sleep(3000);
    const rows = await page.eval(`document.querySelectorAll(".appt").length`);
    await book(first, `Bt Walk-in ${stamp}`);  // another screen, the first doctor: above the rested row
    await page.until(`document.querySelectorAll(".appt").length > ${rows}`, 20000, "the new booking arrives by polling");
    await sleep(300);
    const after = await page.eval(rowAt(rested.id));
    check(Math.abs(after - before) <= 4, `the rested row stayed under the finger (moved ${Math.round(after - before)} px)`);

    // --- 2: a tap on a row that just changed is ignored ---
    await page.eval(`(() => { const r = document.querySelector('.appt[data-id="${tapped.id}"]'); window.scrollBy(0, r.getBoundingClientRect().top - 300); })()`);
    await sleep(400);
    const spot = await page.eval(`(() => { const b = document.querySelector('.appt[data-id="${tapped.id}"] [data-act="arrive"]').getBoundingClientRect();
      return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
    await page.tap(spot.x, spot.y);
    await page.until(`document.querySelector('.appt[data-id="${tapped.id}"]').dataset.status === "waiting"`, 20000, "the row shows the arrival");
    const nowAt = await page.eval(`(() => { const b = document.querySelector('.appt[data-id="${tapped.id}"] [data-act]').getBoundingClientRect();
      return { x: b.left + b.width / 2, y: b.top + b.height / 2, act: document.querySelector('.appt[data-id="${tapped.id}"] [data-act]').dataset.act }; })()`);
    await page.tap(nowAt.x, nowAt.y);  // the same finger again, right away: on "Call in" now
    await sleep(1500);
    const status = (await api("GET", `/api/v1/appointments/${tapped.id}`)).data.appointment.status;
    check(nowAt.act === "call" && status === "waiting", `a tap within 700 ms of the redraw was ignored (button ${nowAt.act}, status ${status})`);
    await page.tap(nowAt.x, nowAt.y);  // a deliberate tap later works
    await sleep(1500);
    const later = (await api("GET", `/api/v1/appointments/${tapped.id}`)).data.appointment.status;
    check(later === "in_room", `a tap after the guard works (status ${later})`);
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e1_rows_stay against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
