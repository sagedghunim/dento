// The booking deposit (Saged, 2026-11-16), as a doctor with a queue today who also holds `settings.manage`
// (HAMADA_BT_USER=ahmed, see README.md) and the desk (HAMADA_BT_DESK, default sara):
// - Team & settings: the Booking card turns the deposit on at EGP 100 (a double-clicked Save saves once), and the
//   follow-up recipe editor offers each session's visit fee and procedure;
// - Book, as the desk, for the doctor's next working day: "Deposit EGP 100" shows ticked with the method chips, it
//   hides with "here now", and a double-clicked Book books once and takes the deposit once;
// - the booking is moved to a free time today through the API (passed times too, so it runs at any hour) and keeps
//   its deposit; the patient arrives and is called in; the doctor records two procedures on the visit screen;
// - the Pay dialog at 390 px: paid is the deposit, due is the total less the deposit, the payment says "Deposit";
// - the receipt prints the deposit with its date.
// The clinic's deposit setting is put back as it was at the end.
// HAMADA_BT_SHOTS=<folder> takes Book at 1280 px and the Pay dialog at 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { BASE, browser, check, freeRoom, signIn, sleep } from "./lib.mjs";

const DESK = process.env.HAMADA_BT_DESK || "sara";
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const stamp = Date.now().toString(36).slice(-5);
const DEPOSIT = 10000;  // EGP 100 in piastres

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

// A money text as the page shows it ("٦٠٠ ج.م", "1,250.50 EGP") to piastres.
const piastresOf = `(text) => { const a = text.replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/٫/g, ".").replace(/[^0-9.]/g, "");
  return Math.round(Number(a) * 100); }`;

const D = `document.getElementById("book-dialog")`;
const BOX = `${D}.querySelector("[data-deposit-box]")`;

// The first working day after today in the month shown (or the next month), then its first free time (as e11).
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

// A day's appointments for a patient name, from the JSON API.
async function bookingsOn(api, name, day) {
  const found = (await api("GET", `/api/v1/today?day=${day}`)).data;
  return found.queues.flatMap((q) => q.appointments).filter((a) => a.patient.name === name && a.status !== "cancelled");
}

export async function run() {
  const doctor = await signIn();
  const desk = await signIn(DESK);
  const clinic = (await doctor.api("GET", "/api/v1/clinic")).data;
  const me = clinic.doctors.find((d) => d.id === doctor.membership);
  if (!me || !me.today.length) throw new Error("This test needs HAMADA_BT_USER to be a doctor working today.");
  if (!clinic.settings) throw new Error("This test needs HAMADA_BT_USER to hold settings.manage.");
  const before = { booking_deposit: clinic.settings.booking_deposit, booking_deposit_amount: clinic.settings.booking_deposit_amount };
  const name = `Bt عربون ${stamp}`;

  const page = await browser({ width: 1280, height: 900 });
  const phone = await browser({ width: 390, height: 844, touch: true });
  let visit = null;
  try {
    // --- Team & settings: the Booking card ---
    if (before.booking_deposit) {
      await doctor.api("PUT", "/api/v1/settings/clinic", { changes: { booking_deposit: false }, expected: { booking_deposit: true } });
    }
    await page.cookie(doctor.session);
    await page.open("/team");
    const card = `document.querySelector("[data-booking-card]")`;
    await page.until(card, 15000, "the Booking card shows");
    check(await page.eval(`!${card}.querySelector('[name="booking_deposit"]').checked && ${card}.querySelector("[data-deposit-amount]").hidden`), "the deposit starts off, with no amount");
    await page.eval(`(() => { const c = ${card}; const on = c.querySelector('[name="booking_deposit"]'); on.checked = true; on.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    check(await page.eval(`!${card}.querySelector("[data-deposit-amount]").hidden`), "turning it on shows the amount");
    await page.eval(`(() => { const c = ${card}; c.querySelector("[data-submit]").click(); })()`);
    await page.until(`${card}.querySelector("[data-error]").textContent.length > 0 && !${card}.dataset.busy`, 5000, "on without an amount is refused on the screen");
    await page.eval(`(() => { const c = ${card}; c.querySelector('[name="booking_deposit_amount"]').value = "100"; const b = c.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!${card}.dataset.busy && ${card}.querySelector("[data-error]").textContent === ""`, 15000, "the setting saves");
    await sleep(500);
    const saved = (await doctor.api("GET", "/api/v1/clinic")).data.settings;
    check(saved.booking_deposit === true && saved.booking_deposit_amount === DEPOSIT, "the deposit is on at EGP 100 (10000 piastres)");

    // The follow-up recipe editor offers each session's visit fee and procedure (looked at, not saved).
    const recipe = `document.getElementById("recipe-dialog")`;
    await page.until(`document.querySelector("[data-recipe-list] li button")`, 15000, "the follow-up recipes load");
    await page.eval(`document.querySelector("[data-recipe-list] li button").click()`);
    await page.until(`${recipe}.open`, 5000, "the recipe dialog opens");
    check(await page.eval(`[...${recipe}.querySelectorAll(".recipe-step")].every((li) => li.querySelectorAll('input[type="radio"]').length === 3
      && li.querySelectorAll('input[type="radio"]:checked').length === 1 && li.querySelector('select[name="procedure_id"]'))`),
      "each session has the three visit fees (one picked) and the procedure done in it");
    await page.eval(`${recipe}.querySelector("[data-close]").click()`);

    // --- Book, as the desk ---
    await freeRoom(desk.api, doctor.membership);
    await page.cookie(desk.session);
    await page.open("/");
    await page.eval(`document.querySelector("[data-open-book]").click()`);
    await page.until(`${D}.open`, 5000, "Book opens");
    await page.eval(`${D}.querySelector("[data-new-patient]").click()`);
    await page.eval(`(() => { const f = ${D}.querySelector('[name="full_name"]'); f.value = ${JSON.stringify(name)}; f.dispatchEvent(new Event("input", { bubbles: true })); })()`);
    await page.eval(`(() => { const i = ${D}.querySelector('[name="doctor"][value="${doctor.membership}"]'); i.checked = true; i.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    await page.until(`${D}.querySelector(".cal-day .cal-hint")?.textContent || ${D}.querySelector(".cal-day.off")`, 8000, "the month's hints load");
    // Still on today: "here now" means a walk-in, who pays at the visit.
    const here = `${D}.querySelector('[name="arrive"]')`;
    if (await page.eval(`Boolean(${here})`)) {
      await page.eval(`(() => { const a = ${here}; a.checked = true; a.dispatchEvent(new Event("change", { bubbles: true })); })()`);
      check(await page.eval(`${BOX}.hidden`), "a patient here now takes no deposit");
      await page.eval(`(() => { const a = ${here}; a.checked = false; a.dispatchEvent(new Event("change", { bubbles: true })); })()`);
      check(await page.eval(`!${BOX}.hidden`), "and it comes back when unticked");
    }
    const when = await pickFutureTime(page);
    console.log(`  booking for ${when.day} ${when.time}`);
    check(await page.eval(`Boolean(${BOX}) && !${BOX}.hidden`), "Book shows the deposit");
    check(await page.eval(`${BOX}.querySelector('[name="deposit"]').checked`), "it is ticked by default");
    const label = await page.eval(`${BOX}.querySelector("[data-deposit-label]").textContent`);
    check(await page.eval(`(${piastresOf})(${JSON.stringify(label)}) === ${DEPOSIT}`), `it says the amount: "${label}"`);
    check(await page.eval(`!${BOX}.querySelector("[data-deposit-methods]").hidden && ${BOX}.querySelectorAll('[name="deposit_method"]').length > 1`), "with the payment-method chips");
    await page.eval(`(() => { const d = ${BOX}.querySelector('[name="deposit"]'); d.checked = false; d.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    check(await page.eval(`${BOX}.querySelector("[data-deposit-methods]").hidden`), "the desk can untick it");
    await page.eval(`(() => { const d = ${BOX}.querySelector('[name="deposit"]'); d.checked = true; d.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    await page.eval(`(() => { const m = ${BOX}.querySelector('[name="deposit_method"][value="cash"]'); if (m) { m.checked = true; m.dispatchEvent(new Event("change", { bubbles: true })); } })()`);
    await page.eval(`${BOX}.scrollIntoView({ block: "center" })`);
    await shot(page, "v2-book-deposit-1280.png");
    await page.eval(`(() => { const b = ${D}.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!${D}.open || (!${D}.dataset.busy && ${D}.querySelector("[data-error]").textContent)`, 15000, "Book answers");
    const refused = await page.eval(`${D}.open ? ${D}.querySelector("[data-error]").textContent : ""`);
    check(!refused, `the booking is made and the dialog closes ${refused}`);
    await sleep(500);
    const booked = await bookingsOn(desk.api, name, when.day);
    check(booked.length === 1, "a double-clicked Book books once");
    visit = booked[0];
    check(visit.invoice && visit.invoice.paid === DEPOSIT, "the deposit is paid on the visit's bill, once");
    let bill = (await desk.api("GET", `/api/v1/invoices/${visit.invoice.id}`)).data.invoice;
    check(bill.payments.length === 1 && bill.payments[0].deposit === true && bill.payments[0].amount === DEPOSIT, "one payment, marked as the deposit");
    check(bill.due === bill.total - DEPOSIT, `due is the fee less the deposit (${bill.total} − ${DEPOSIT} = ${bill.due})`);

    // --- the visit comes today: moved to a free time of today (passed ones too, which only the API offers, so the
    // test runs at any hour), then arrived and called in; the deposit moves with it ---
    const today = (await desk.api("GET", `/api/v1/doctors/${doctor.membership}/slots`)).data;
    const free = today.slots.find((s) => !s.taken);
    if (!free && !today.queue) throw new Error("This test needs a free time today for HAMADA_BT_USER.");
    let r = await desk.api("POST", `/api/v1/appointments/${visit.id}/reschedule`, {
      status: "booked", doctor_id: doctor.membership, day: today.day, ...(free ? { starts_at: free.starts_at } : {}) });
    check(r.status === 200 && r.data.appointment.invoice.paid === DEPOSIT, `moved to today ${free ? free.starts_at.slice(11, 16) : "(queue)"} with its deposit`);
    visit = r.data.appointment;
    r = await desk.api("POST", `/api/v1/appointments/${visit.id}/arrive`, { status: "booked" });
    check(r.status === 200, "the patient is marked arrived");
    r = await desk.api("POST", `/api/v1/appointments/${visit.id}/call`, { status: "waiting" });
    if (r.status !== 200) throw new Error(`call-in failed: ${r.status} ${JSON.stringify(r.data)}`);
    await page.cookie(doctor.session);
    await page.open(`/visits/${visit.id}`);
    const chips = `[...document.querySelectorAll("[data-add-procedure] [data-proc]")]`;
    await page.until(`${chips}.length >= 2`, 10000, "the visit screen offers procedures");
    for (let i = 0; i < 2; i++) {
      const count = await page.eval(`document.querySelectorAll("[data-procedures] li").length`);
      await page.eval(`${chips}[${i}].click()`);
      await sleep(300);
      // A tooth procedure asks for the tooth (optional): one more tap records it.
      await page.eval(`(() => { const c = document.querySelector("[data-proc-confirm]"); if (c && !c.hidden) c.querySelector("[data-submit]").click(); })()`);
      await page.until(`document.querySelectorAll("[data-procedures] li").length === ${count + 1}`, 10000, `procedure ${i + 1} is recorded`);
    }
    await page.eval(`window.onbeforeunload = null`);
    bill = (await desk.api("GET", `/api/v1/invoices/${visit.invoice.id}`)).data.invoice;
    check(bill.lines.length === 3 && bill.paid === DEPOSIT, `the bill has the visit and two procedures (total ${bill.total}), paid the deposit`);

    // --- the Pay dialog at 390 px, as the desk ---
    await phone.cookie(desk.session);
    await phone.open("/");
    await phone.eval(`(() => { const all = document.querySelector('[data-filter-doctor=""]'); if (all) all.click(); })()`);
    const row = `document.querySelector('.appt[data-id="${visit.id}"]')`;
    await phone.until(row, 15000, "the visit's row shows on Today");
    await sleep(900);  // a tap right after a row's redraw is ignored
    await phone.eval(`${row}.querySelector('[data-act="invoice"]').click()`);
    const pay = `document.getElementById("pay-dialog")`;
    await phone.until(`${pay}.open && ${pay}.querySelectorAll("[data-payments] li").length === 1`, 15000, "the Pay dialog shows the bill");
    const sum = async (key) => phone.eval(`(${piastresOf})(${pay}.querySelector('[data-sum="${key}"]').textContent)`);
    const [total, paid, due] = [await sum("total"), await sum("paid"), await sum("due")];
    check(total === bill.total, `Pay shows the total (${total})`);
    check(paid === DEPOSIT, `Pay shows the deposit as paid (${paid})`);
    check(due === total - DEPOSIT && due === bill.due, `due = total − deposit: ${total} − ${DEPOSIT} = ${due}`);
    check(await phone.eval(`${pay}.querySelector("[data-credit-row]").hidden`), "no credit row when the bill is more than the deposit");
    const payment = await phone.eval(`${pay}.querySelector("[data-payments] li").textContent`);
    check(payment.includes("عربون") || payment.includes("Deposit"), `the payment says it is the deposit: "${payment.trim()}"`);
    check(await phone.eval(`(${piastresOf})(${pay}.querySelector('[name="amount"]').value) === ${due}`), "the amount to collect starts at what is due");
    check(await phone.eval(`document.documentElement.scrollWidth <= 390`), "the dialog fits 390 px");
    await phone.eval(`${pay}.querySelector("[data-sum]").scrollIntoView({ block: "start" })`);
    await shot(phone, "v2-pay-deposit-390.png");

    // --- the receipt ---
    await phone.open(`/print/receipts/${visit.invoice.id}`);
    const rows = await phone.eval(`[...document.querySelectorAll("[data-payment]")].map((r) => ({ deposit: r.hasAttribute("data-deposit"), text: r.textContent }))`);
    check(rows.length === 1 && rows[0].deposit && (rows[0].text.includes("عربون") || rows[0].text.includes("Deposit")), "the receipt lists the deposit");
    const day = bill.payments[0].day;
    const dayNumber = String(Number(day.slice(8, 10)));
    const dayArabic = dayNumber.replace(/[0-9]/g, (d) => "٠١٢٣٤٥٦٧٨٩"[d]);
    check(rows[0].text.includes(dayNumber) || rows[0].text.includes(dayArabic), `with its date (${day}): "${rows[0].text.trim()}"`);
    check(page.errors().length === 0 && phone.errors().length === 0, `no script errors ${[...page.errors(), ...phone.errors()].join(" | ")}`);
  } finally {
    if (visit) await desk.api("POST", `/api/v1/appointments/${visit.id}/finish`, { status: "in_room" });
    const now = (await doctor.api("GET", "/api/v1/clinic")).data.settings;
    const changes = {};
    for (const key of Object.keys(before)) if (now[key] !== before[key]) changes[key] = before[key];
    if (Object.keys(changes).length) {
      await doctor.api("PUT", "/api/v1/settings/clinic", { changes, expected: Object.fromEntries(Object.keys(changes).map((k) => [k, now[k]])) });
    }
    page.close();
    phone.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e15_deposit against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
