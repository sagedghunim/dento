// Treatment plans and follow-ups (Saged, 2026-11-13), signed in as a doctor with a queue today (HAMADA_BT_USER=ahmed);
// the desk is HAMADA_BT_DESK (default sara).
// The visit screen: "Done today" is a small bill; one of the six quick procedures, double-clicked, is recorded once at
// the list price; a procedure found by typing offers its follow-up sessions, added once; a session's day changes with
// one chip; "what comes next" takes a manual follow-up in one tap, once through a double click and a dropped
// connection; the server refuses a doctor's own price. After the finish the desk's Follow-ups list shows the patient
// with the sessions, the phone and WhatsApp; "Book" opens the Book dialog on the patient and the doctor; a linked
// booking leaves the list; an expired session goes to sign-in; "Later" (double-clicked) puts the row off once.
// At 390 px nothing scrolls sideways. With HAMADA_BT_SHOTS=<folder> it takes the visit at 1280 px and Follow-ups at
// 1280 and 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { browser, check, freeRoom, inArabic, signIn, sleep } from "./lib.mjs";

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

const offline = (page, on) => page.cdp("Network.emulateNetworkConditions", { offline: on, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
const nextCount = `document.querySelectorAll("[data-upcoming] li").length`;

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
  const { session, membership, api } = await signIn();
  const desk = await signIn(DESK);
  const config = (await api("GET", "/api/v1/clinic")).data;
  const service = config.services.find((s) => s.kind === "procedure").id;
  await freeRoom(api, membership);
  const name = `Bt Plan ${stamp}`;
  const phoneNo = `0101${String(Date.now()).slice(-7)}`;
  const made = await api("POST", "/api/v1/appointments/walk-in", { doctor_id: membership, service_id: service,
                                                          new_patient: { full_name: name, phone: phoneNo, sex: "m", age: 30 } });
  check(made.status === 201, `a walk-in for the doctor (${made.status} ${JSON.stringify(made.data).slice(0, 200)})`);
  const visit = made.data.appointment;
  check((await api("POST", `/api/v1/appointments/${visit.id}/call`, { status: "waiting" })).status === 200, "called in");
  const record = async () => (await api("GET", `/api/v1/appointments/${visit.id}/record`)).data;
  const planned = async () => (await record()).open_plan_items.filter((i) => i.due_on);

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open(`/visits/${visit.id}`);
    await page.until(`Boolean(document.querySelector("[data-add-procedure]"))`, 10000, "the visit screen opens");
    const data = await page.eval(`JSON.parse(document.getElementById("visit-data").textContent)`);
    check((await page.eval(`document.querySelectorAll("[data-top-procedures] [data-proc]").length`)) === 6, "six one-tap procedures");
    check((await page.eval(`document.querySelector("[data-notes]").placeholder`)).includes("الشكوى"), "the notes say what to write");
    check(!(await page.eval(`Boolean(document.querySelector('[name="unit_price"], [name="price"]'))`)), "no price field");
    check((await page.eval(`document.querySelector("#procs-title").textContent`)).trim() === "اللي اتعمل النهارده", "the card is the day's bill");

    // One tap on a quick procedure, double-clicked: recorded once, at the list price, and the total follows.
    const quick = data.procedures.find((p) => data.top.includes(p.id) && p.unit !== "tooth");
    const before = (await record()).procedures.length;
    await page.eval(`(() => { const b = document.querySelector('[data-top-procedures] [data-proc="${quick.id}"]'); b.click(); b.click(); })()`);
    await page.until(`document.querySelectorAll("[data-procedures] li").length === ${before + 1}`, 10000, `one tap records it (${quick.name})`);
    await sleep(600);
    let rec = await record();
    check(rec.procedures.length === before + 1, "a double click recorded one procedure");
    check(rec.procedures.at(-1).unit_price === quick.price, "at the clinic's list price");
    // The bill's total only for someone with money.view (Saged, 2026-11-16): a plain doctor sees no amount.
    const totalShown = await page.eval(`document.querySelector("[data-procs-total]")?.textContent ?? null`);
    if (totalShown === null) check(!rec.invoice, "a doctor without money.view sees no total and no invoice");
    else check(totalShown.replace(/[^0-9٠-٩]/g, "").length > 0 && rec.invoice.total === visit.fee + quick.price, `the bill adds the consultation and the procedure (${totalShown})`);
    // Another procedure (this one is on the visit already: a second one is "already added", Saged 2026-11-17), at a
    // price of the doctor's own. A doctor without money.view isn't told the list price, so any amount will do.
    const other = data.procedures.find((p) => p.id !== quick.id && p.unit !== "tooth");
    const refused = await api("POST", `/api/v1/appointments/${visit.id}/procedures`, { procedure_id: other.id, unit_price: 123 });
    const again = await api("POST", `/api/v1/appointments/${visit.id}/procedures`, { procedure_id: quick.id });
    check(again.status === 409 && again.data.error.code === "already_added", "the same procedure again is already added");
    check(refused.status === 403 && refused.data.error.code === "price_locked", "the server refuses a doctor's own price");

    // A procedure found by typing, with a recipe: its sessions are offered and added once.
    const recipe = data.procedures.find((p) => p.follow_up.length === 2 && !data.top.includes(p.id)) || data.procedures.find((p) => p.follow_up.length === 2);
    if (data.top.includes(recipe.id)) {
      await page.eval(`document.querySelector('[data-top-procedures] [data-proc="${recipe.id}"]').click()`);
    } else {
      await page.eval(`(() => { const s = document.querySelector("[data-proc-search]"); s.value = ${JSON.stringify(recipe.name.slice(0, 6))};
                                s.dispatchEvent(new Event("input", { bubbles: true })); })()`);
      await page.until(`Boolean(document.querySelector('[data-proc-results] [data-proc="${recipe.id}"]'))`, 3000, "typing finds the procedure");
      await page.eval(`document.querySelector('[data-proc-results] [data-proc="${recipe.id}"]').click()`);
    }
    if (recipe.unit === "tooth") {
      await page.until(`!document.querySelector("[data-proc-confirm]").hidden`, 3000, "a tooth procedure asks for the tooth, optional");
      await page.eval(`document.querySelector("[data-proc-confirm] [data-submit]").click()`);
    }
    await page.until(`Boolean(document.querySelector("[data-offers] .offer"))`, 10000, `the recipe is offered (${recipe.name})`);
    check((await page.eval(`document.querySelector("[data-offers] .offer p").textContent`)).includes("جلستين"), "the offer says two sessions and when");
    await page.eval(`(() => { const b = document.querySelector("[data-offers] [data-offer-add]"); b.click(); b.click(); })()`);
    await page.until(`${nextCount} === 2`, 10000, "one tap adds the two sessions");
    await sleep(600);
    let items = await planned();
    check(items.length === 2, "added once on the server");
    check(!(await page.eval(`Boolean(document.querySelector("[data-offers] .offer"))`)), "the offer is gone");

    // The day of a session changes with one chip.
    const firstDue = items[0].due_on;
    await page.eval(`document.querySelector("[data-upcoming] li .chip-btn").click()`);
    await page.until(`Boolean(document.querySelector("[data-upcoming] .next-when"))`, 2000, "the interval chips open");
    await page.eval(`document.querySelector('[data-upcoming] .next-when [data-days="30"]').click()`);
    await page.until(`!document.querySelector("[data-upcoming] .next-when")`, 10000, "the list redraws");
    items = await planned();
    const monthOn = new Date(Date.parse(`${data.today}T00:00:00Z`) + 30 * 86400000).toISOString().slice(0, 10);
    check(!items.some((i) => i.due_on === firstDue) && items.some((i) => i.due_on === monthOn), `the session moved to a month on (${monthOn})`);

    // A manual follow-up: "what" is already picked, one tap on "when" adds it. Double-clicked: once.
    await page.eval(`(() => { const b = document.querySelector('[data-next-when] [data-days="7"]'); b.click(); b.click(); })()`);
    await page.until(`${nextCount} === 3`, 10000, "a manual follow-up in one tap");
    await sleep(600);
    check((await planned()).length === 3, "a double click added one");

    // A dropped connection: it waits for "Try again" and is added once.
    await offline(page, true);
    await page.eval(`document.querySelector('[data-next-when] [data-days="2"]').click()`);
    await page.until(`!document.querySelector("[data-next-add] [data-submit]").hidden`, 45000, "offline: it offers Try again");
    await offline(page, false);
    await page.eval(`document.querySelector("[data-next-add] [data-submit]").click()`);
    await page.until(`${nextCount} === 4`, 20000, "added after the connection is back");
    await sleep(600);
    check((await planned()).length === 4, "added once after the drop");
    check(await page.eval(`document.querySelector("[data-next-add] [data-submit]").hidden`), "Try again hides");

    // The doctor takes one back.
    await page.eval(`document.querySelector("[data-upcoming] li .icon-btn").click()`);
    await page.until(`${nextCount} === 3`, 10000, "a removed follow-up leaves the list");
    check((await planned()).length === 3, "dropped on the server");

    // Not on the desk's list before the finish.
    const early = await (await fetch((await page.eval("location.origin")) + "/follow-ups", { headers: { Cookie: `hamada_session=${desk.session}` } })).text();
    check(!early.includes(name), "the desk doesn't see them before the visit is finished");

    await page.eval(`window.scrollTo(0, document.querySelector("[data-procs-card]").offsetTop - 12)`);
    await shot(page, "v2-plan-visit-1280.png");

    await page.eval(`document.querySelector("[data-finish]").click()`);
    await page.until(`location.pathname === "/"`, 15000, "the finish goes back to Today");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }

  // The desk's Follow-ups list.
  const row = `[...document.querySelectorAll("[data-fu]")].find((r) => r.dataset.name === ${JSON.stringify(name)})`;
  const deskPage = await browser({ width: 1280, height: 900 });
  try {
    await deskPage.cookie(desk.session);
    await deskPage.open("/follow-ups");
    await deskPage.until(`Boolean(${row})`, 8000, "the finished visit's follow-ups are on the desk's list");
    check((await deskPage.eval(`document.querySelector('.main-nav [aria-current="page"]').textContent`)).trim() === "المتابعات", "Follow-ups is in the side bar");
    check((await deskPage.eval(`[...document.querySelectorAll("[data-fu]")].filter((r) => r.dataset.name === ${JSON.stringify(name)}).reduce((n, r) => n + r.querySelectorAll(".fu-items li").length, 0)`)) === 3, "the three sessions are listed");
    check(await deskPage.eval(`Boolean(${row}.querySelector('a[href="tel:${phoneNo}"]')) && Boolean(${row}.querySelector('a[href^="https://wa.me/2"]'))`), "the phone calls and opens WhatsApp");
    await shot(deskPage, "v2-followups-1280.png");

    // "Book" opens the Book dialog on the patient and the doctor.
    await deskPage.eval(`${row}.querySelector("[data-fu-book]").click()`);
    await deskPage.until(`document.getElementById("book-dialog").open`, 5000, "Book opens the dialog");
    check((await deskPage.eval(`document.querySelector("#book-dialog [data-picked-name]").textContent`)).includes(name), "on the patient");
    check((await deskPage.eval(`(document.querySelector('#book-dialog [name="doctor"]:checked') || {}).value`)) === String(membership), "and the doctor");
    await deskPage.eval(`document.querySelector("#book-dialog [data-close]").click()`);

    // A booking linked to the first session: it leaves the list.
    const firstItem = Number(await deskPage.eval(`${row}.querySelector("[data-item]").dataset.item`));
    const again = await desk.api("POST", "/api/v1/appointments/walk-in", { doctor_id: membership, service_id: service, patient_id: visit.patient.id });
    check(again.status === 201, "the desk books the patient again");
    check((await desk.api("POST", "/api/v1/follow-ups/booked", { item_ids: [firstItem], appointment_id: again.data.appointment.id })).status === 200, "the booking is linked");
    await deskPage.open("/follow-ups");
    check((await deskPage.eval(`[...document.querySelectorAll("[data-fu]")].filter((r) => r.dataset.name === ${JSON.stringify(name)}).reduce((n, r) => n + r.querySelectorAll(".fu-items li").length, 0)`)) === 2, "the booked session left the list");


    // At 390 px: nothing sideways, big targets.
    const phone = await browser({ width: 390, height: 844, touch: true });
    try {
      await phone.cookie(desk.session);
      await phone.open("/follow-ups");
      check(await phone.eval(`document.documentElement.scrollWidth <= 390`), "Follow-ups fits 390 px");
      const small = await phone.eval(`[...document.querySelectorAll("[data-fu] button, [data-fu] a.tel")].map((b) => b.getBoundingClientRect().height).filter((h) => h < 44).length`);
      check(small === 0, "every tap target is at least 44 px high");
      await shot(phone, "v2-followups-390.png");
      check(phone.errors().length === 0, `no script errors (${phone.errors().join(" | ")})`);
    } finally {
      phone.close();
    }

    // An expired session: sign-in, and nothing is lost.
    await deskPage.cdp("Network.deleteCookies", { name: "hamada_session", url: (await deskPage.eval("location.origin")) });
    await deskPage.eval(`${row}.querySelector("[data-fu-later]").click()`);
    await deskPage.until(`location.pathname === "/login"`, 15000, "an expired session goes to sign-in");
    await deskPage.cookie(desk.session);
    await deskPage.open("/follow-ups");
    await deskPage.until(`Boolean(${row})`, 8000, "the row is still there after signing in");

    // "Later", double-clicked: every row of the patient is put off, each once.
    const rows = await deskPage.eval(`[...document.querySelectorAll("[data-fu]")].filter((r) => r.dataset.name === ${JSON.stringify(name)}).length`);
    for (let i = 0; i < rows; i++) {
      await deskPage.eval(`(() => { const b = ${row}.querySelector("[data-fu-later]"); b.click(); b.click(); })()`);
      await deskPage.until(`[...document.querySelectorAll("[data-fu]")].filter((r) => r.dataset.name === ${JSON.stringify(name)}).length === ${rows - i - 1}`, 10000, "Later takes the row off the list");
    }
    await deskPage.open("/follow-ups");
    check(!(await deskPage.eval(`Boolean(${row})`)), "it stays off after a reload");
    check(deskPage.errors().length === 0, `no script errors (${deskPage.errors().join(" | ")})`);
  } finally {
    deskPage.close();
  }
  const leftover = await desk.api("GET", "/api/v1/today");
  for (const a of leftover.data.queues.flatMap((q) => q.appointments)) {
    if (a.patient.name === name && ["waiting", "arrived"].includes(a.status)) await desk.api("POST", `/api/v1/appointments/${a.id}/cancel`, { status: a.status });
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
