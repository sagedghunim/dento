// Discount types (Saged, 2026-11-13), as a doctor with a queue today who also holds `settings.manage`
// (HAMADA_BT_USER=ahmed after granting it, see README.md) and the desk (HAMADA_BT_DESK, default sara):
// - Team & settings: a discount type is added once on a double-clicked Save, edited, and a fixed one is added;
// - the visit screen: one tap suggests a type, the chip is pressed, and no money changes;
// - the Pay dialog at 390 px: the desk sees "The doctor suggested", the chip is marked, a double tap applies the
//   percent once, a tap on the applied chip removes it, and the desk has no typed amount;
// - the receipt prints "Discount (name)". The test stops its own types at the end.
// HAMADA_BT_SHOTS=<folder> takes Team & settings at 1280 px and the Pay dialog at 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { BASE, browser, check, freeRoom, signIn, sleep } from "./lib.mjs";

const DESK = process.env.HAMADA_BT_DESK || "sara";
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const stamp = Date.now().toString(36).slice(-5);

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const dialog = `document.getElementById("discount-dialog")`;
const listText = `document.querySelector("[data-discount-list]").textContent`;

export async function run() {
  const doctor = await signIn();
  const desk = await signIn(DESK);
  const config = (await desk.api("GET", "/api/v1/clinic")).data;
  const me = config.doctors.find((d) => d.id === doctor.membership);
  if (!me || !me.today.length) throw new Error("This test needs HAMADA_BT_USER to be a doctor working today.");
  const service = config.services.find((s) => s.kind === "consultation").id;
  const percentName = `Bt خصم ${stamp}`;
  const fixedName = `Bt ثابت ${stamp}`;
  const mine = async () => (await doctor.api("GET", "/api/v1/discount-types")).data.items.filter((d) => d.name.endsWith(stamp));

  const page = await browser({ width: 1280, height: 900 });
  const phone = await browser({ width: 390, height: 844, touch: true });
  let visit = null;
  try {
    // --- Team & settings: add (double-clicked), edit, add a fixed one ---
    await page.cookie(doctor.session);
    await page.open("/team");
    await page.until(`document.querySelector("[data-discount-card]") && !document.querySelector("[data-discount-status]").textContent.includes("…")`, 15000, "the discount card loads");
    await page.eval(`document.querySelector("[data-discount-add]").click()`);
    await page.until(`${dialog}.open`, 5000, "the add dialog opens");
    await page.eval(`(() => { const f = ${dialog}.querySelector("form"); f.elements.namedItem("name").value = ${JSON.stringify(percentName)};
      f.elements.namedItem("value").value = "10"; const b = f.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!${dialog}.open && ${listText}.includes(${JSON.stringify(percentName)})`, 15000, "the new type is listed");
    check((await mine()).length === 1, "a double-clicked Save adds the type once");
    check(await page.eval(`${listText}.includes("10%") || ${listText}.includes("١٠٪")`), "the list shows its percent");

    await page.eval(`[...[...document.querySelectorAll("[data-discount-list] li")].find((li) => li.textContent.includes(${JSON.stringify(percentName)})).querySelectorAll("button")].find((b) => b.textContent === document.querySelector('#strings [data-k="recipe_edit"]').textContent).click()`);
    await page.until(`${dialog}.open && ${dialog}.querySelector('[name="name"]').value === ${JSON.stringify(percentName)}`, 5000, "the edit dialog opens on the type");
    check(await page.eval(`!${dialog}.querySelector("[data-discount-stop]").hidden`), "editing offers Stop using it");
    await page.eval(`(() => { const f = ${dialog}.querySelector("form"); f.elements.namedItem("value").value = "15"; f.querySelector("[data-submit]").click(); })()`);
    await page.until(`!${dialog}.open`, 15000, "the edit saves");
    check((await mine())[0].value === 15, "the percent was changed to 15");

    await page.eval(`document.querySelector("[data-discount-add]").click()`);
    await page.until(`${dialog}.open`, 5000, "the add dialog opens again");
    check(await page.eval(`${dialog}.querySelector('[name="name"]').value === "" && ${dialog}.querySelector("[data-discount-stop]").hidden`), "it opens empty, with no Stop");
    await page.eval(`(() => { const f = ${dialog}.querySelector("form"); f.elements.namedItem("name").value = ${JSON.stringify(fixedName)};
      const fixed = f.querySelector('[name="kind"][value="fixed"]'); fixed.checked = true; fixed.dispatchEvent(new Event("change", { bubbles: true }));
      f.elements.namedItem("value").value = "50"; f.querySelector("[data-submit]").click(); })()`);
    await page.until(`!${dialog}.open && ${listText}.includes(${JSON.stringify(fixedName)})`, 15000, "the fixed type is listed");
    const types = await mine();
    const percent = types.find((d) => d.name === percentName);
    const fixed = types.find((d) => d.name === fixedName);
    check(fixed && fixed.kind === "fixed" && fixed.value === 5000, "a fixed amount of 50 is stored as 5000 piastres");
    await page.eval(`document.querySelector("[data-discount-card]").scrollIntoView({ block: "center" })`);
    await shot(page, "v2-discount-types-1280.png");

    // --- the visit screen: the doctor suggests ---
    await freeRoom(desk.api, doctor.membership);
    const walk = await desk.api("POST", "/api/v1/appointments/walk-in", {
      doctor_id: doctor.membership, service_id: service, new_patient: { full_name: `Bt Discount ${stamp}`, phone: `0101${String(Date.now()).slice(-7)}`, sex: "m", age: 30 } });
    if (walk.status !== 201) throw new Error(`walk-in failed: ${walk.status} ${JSON.stringify(walk.data)}`);
    visit = walk.data.appointment;
    const called = await desk.api("POST", `/api/v1/appointments/${visit.id}/call`, { status: visit.status });
    if (called.status !== 200) throw new Error(`call-in failed: ${called.status} ${JSON.stringify(called.data)}`);
    await page.open(`/visits/${visit.id}`);
    const chip = `document.querySelector('[data-suggest-discount] [data-type="${percent.id}"]')`;
    await page.until(`${chip}`, 10000, "the visit screen offers the discount types");
    check(await page.eval(`${chip}.getAttribute("aria-pressed") === "false"`), "nothing is suggested at first");
    // "Suggests discounts" without "Sees prices and money": the names only (Saged, 2026-11-16).
    check(await page.eval(`!${chip}.querySelector(".count") && !/[%٪]/.test(document.querySelector("[data-suggest-discount]").textContent)`),
      "the chips name the types without their percent");
    await page.eval(`${chip}.click()`);
    await page.until(`${chip}.getAttribute("aria-pressed") === "true"`, 10000, "one tap suggests the type");
    let offered = (await desk.api("GET", `/api/v1/appointments/${visit.id}/discount-types`)).data;
    check(offered.suggested && offered.suggested.id === percent.id, "the desk is told what the doctor suggested");
    await page.eval(`${chip}.click()`);
    await page.until(`${chip}.getAttribute("aria-pressed") === "false"`, 10000, "a second tap clears it");
    await page.eval(`${chip}.click()`);
    await page.until(`${chip}.getAttribute("aria-pressed") === "true"`, 10000, "and a third suggests it again");
    const before = (await desk.api("POST", `/api/v1/appointments/${visit.id}/invoice`)).data.invoice;
    check(before.discount === 0 && before.discount_type === null, "the suggestion changed no money");
    await page.eval(`window.onbeforeunload = null`);

    // --- the Pay dialog at 390 px, as the desk ---
    await phone.cookie(desk.session);
    await phone.open("/");
    await phone.eval(`(() => { const all = document.querySelector('[data-filter-doctor=""]'); if (all) all.click(); })()`);
    const row = `document.querySelector('.appt[data-id="${visit.id}"]')`;
    await phone.until(row, 15000, "the visit's row shows on Today");
    await sleep(900);  // a tap right after a row's redraw is ignored
    await phone.eval(`${row}.querySelector('[data-act="invoice"]').click()`);
    const pay = `document.getElementById("pay-dialog")`;
    const payChip = `${pay}.querySelector('[data-discount-types] [data-type="${percent.id}"]')`;
    await phone.until(`${pay}.open && ${payChip}`, 15000, "the Pay dialog shows the discount chips");
    check(await phone.eval(`${pay}.querySelector("[data-suggested]").textContent.includes(${JSON.stringify(percentName)}) && !${pay}.querySelector("[data-suggested]").hidden`), "it says what the doctor suggested");
    check(await phone.eval(`${payChip}.classList.contains("suggested")`), "the suggested chip is marked");
    check(await phone.eval(`!${pay}.querySelector('[name="discount"]')`), "the desk has no typed amount");
    await phone.eval(`(() => { const b = ${payChip}; b.click(); b.click(); })()`);
    await phone.until(`${payChip} && ${payChip}.getAttribute("aria-pressed") === "true"`, 15000, "a tap applies the type");
    let invoice = (await desk.api("GET", `/api/v1/invoices/${before.id}`)).data.invoice;
    const expected = Math.floor((invoice.subtotal * 15 + 50) / 100);
    check(invoice.discount === expected && invoice.discount_type.id === percent.id, `15% of the bill, once (${invoice.discount} of ${invoice.subtotal})`);
    check(invoice.total === invoice.subtotal - expected, "the total follows");
    check(await phone.eval(`document.documentElement.scrollWidth <= 390`), "the dialog fits 390 px");
    await phone.eval(`${payChip}.scrollIntoView({ block: "center" })`);
    await shot(phone, "v2-pay-discount-390.png");
    await phone.eval(`${payChip}.click()`);
    await phone.until(`${payChip} && ${payChip}.getAttribute("aria-pressed") === "false"`, 15000, "a tap on the applied chip removes it");
    invoice = (await desk.api("GET", `/api/v1/invoices/${before.id}`)).data.invoice;
    check(invoice.discount === 0 && invoice.discount_type === null, "the discount is gone");
    await phone.eval(`${payChip}.click()`);
    await phone.until(`${payChip} && ${payChip}.getAttribute("aria-pressed") === "true"`, 15000, "and it applies again");

    // --- the receipt names it ---
    await phone.open(`/print/receipts/${before.id}`);
    check(await phone.eval(`document.querySelector(".totals").textContent.includes(${JSON.stringify(percentName)})`), "the receipt prints the discount's name");
    check(page.errors().length === 0 && phone.errors().length === 0, `no script errors ${[...page.errors(), ...phone.errors()].join(" | ")}`);
  } finally {
    for (const d of await mine()) await doctor.api("PATCH", `/api/v1/discount-types/${d.id}`, { is_active: false });
    if (visit) await desk.api("POST", `/api/v1/appointments/${visit.id}/finish`, { status: "in_room" });
    page.close();
    phone.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e14_discounts against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
