// A new patient (Saged, 2026-11-17), as the desk (sara), at 390 px: Walk-in's new-patient form asks for a three-part
// name, a mobile, the sex and an age; typing a name already on file offers that file; an age under 16 proposes a
// guardian's number; a number that is already someone's own is refused with "Pick it"; the same patient again today
// is refused with "Go to it"; a double-clicked Add books once. HAMADA_BT_SHOTS=<folder> takes the form at 390 px.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { bookingsOf, browser, check, signIn, sleep } from "./lib.mjs";

const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const W = `document.getElementById("walkin-dialog")`;
const toastText = `document.querySelector("#toast.show [data-text]") ? document.querySelector("#toast [data-text]").textContent : ""`;

export async function run() {
  const { session, api } = await signIn();
  const stamp = Date.now().toString(36).slice(-5);
  const phone = `015${String(Date.now()).slice(-8)}`;
  const name = `Bt Nour ${stamp}`;
  const page = await browser({ width: 390, height: 844, touch: true });
  try {
    await page.cookie(session);
    await page.open("/");
    const open = async () => {
      await page.eval(`[...document.querySelectorAll("[data-open-walkin]")].find((b) => b.offsetParent !== null).click()`);
      await page.until(`${W}.open`, 3000, "Walk-in opens");
      await page.eval(`${W}.querySelector("[data-new-patient]").click()`);
      await page.until(`!${W}.querySelector("[data-new]").hidden`, 3000, "the new-patient form shows");
      // No payment here: this test is about the patient (Collect now starts ticked for a walk-in).
      await page.eval(`(() => { const c = ${W}.querySelector('[name="collect"]'); if (c && c.checked) c.click(); })()`);
    };
    const type = (field, value) => page.eval(`(() => { const f = ${W}.querySelector('[name="${field}"]'); f.value = ${JSON.stringify(value)};
      f.dispatchEvent(new Event("input", { bubbles: true })); f.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    const error = () => page.eval(`${W}.querySelector("[data-error]").textContent`);

    await open();
    // Nothing filled: the screen names the first thing missing, and nothing is sent.
    await type("full_name", `Bt ${stamp}`);
    await page.eval(`${W}.querySelector("[data-submit]").click()`);
    check((await error()) !== "", `two names are refused on the screen: "${await error()}"`);
    await type("full_name", name);
    await type("phone", phone);
    await type("age", "7");
    check(await page.eval(`${W}.querySelector('[name="phone_owner"][value="guardian"]').checked`), "under 16: the guardian's number is proposed");
    check(await page.eval(`!${W}.querySelector("[data-np-relation]").hidden`), "the guardian's relation shows");
    await page.eval(`${W}.querySelector('[name="guardian_relation"][value="mother"]').click()`);
    await page.eval(`${W}.querySelector('[name="sex"][value="f"]').click()`);
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "the form fits 390 px");
    if (SHOTS) {
      await sleep(300);
      const r = await page.cdp("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(SHOTS, "new-patient-390.png"), Buffer.from(r.result.data, "base64"));
      console.log("  screenshot: new-patient-390.png");
    }
    await page.eval(`(() => { const b = ${W}.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!${W}.open`, 10000, "the dialog closes after the walk-in");
    check((await page.eval(toastText)).includes(name), "the toast names the patient");
    await sleep(500);
    const booked = await bookingsOf(api, name);
    check(booked.length === 1, "the double click booked once");
    const file = (await api("GET", `/api/v1/patients/${booked[0].patient.id}`)).data.patient;
    check(file.phone_owner === "guardian" && file.guardian_relation === "mother" && file.sex === "f" && file.age === 7,
      "the file keeps the guardian's number (mother), the sex and the age");

    // The same name typed again: the file is offered while typing.
    await open();
    await type("full_name", `bt nour ${stamp}`);
    await page.until(`!${W}.querySelector("[data-np-suggest]").hidden`, 5000, "the file on record is suggested");
    // The patient is in the clinic now: picking the file and adding again is refused, with "Go to it".
    await page.eval(`${W}.querySelector("[data-np-suggest] [data-pick-patient]").click()`);
    await page.eval(`${W}.querySelector("[data-submit]").click()`);
    await page.until(`!${W}.querySelector("[data-conflict]").hidden`, 8000, "the second visit today is refused");
    const said = await page.eval(`${W}.querySelector("[data-conflict-text]").textContent`);
    check(said.includes(name) || said.includes(`Bt Nour`), `the refusal names the patient: "${said}"`);
    check(await page.eval(`!${W}.querySelector("[data-conflict-go]").hidden`), "it offers Go to it");
    check((await bookingsOf(api, name)).length === 1, "nothing more was booked");
    await page.eval(`${W}.querySelector("[data-close]").click()`);

    // The mother registers with her own number: allowed (her children's files have it as a guardian's). A second
    // adult with it as their own is refused before anything is sent twice, with the owner's file to pick.
    const adult = async (who) => {
      await open();
      await type("full_name", who);
      await type("phone", phone);
      await type("age", "35");
      await page.eval(`${W}.querySelector('[name="sex"][value="f"]').click()`);
      await page.eval(`${W}.querySelector("[data-submit]").click()`);
    };
    await adult(`Bt Mother ${stamp}`);
    await page.until(`!${W}.open`, 10000, "the mother's own file is made");
    await adult(`Bt Aunt ${stamp}`);
    await page.until(`!${W}.querySelector("[data-np-phone]").hidden && ${W}.querySelector("[data-np-phone] [data-pick-patient]")`, 8000,
      "the number's owner is offered");
    check((await bookingsOf(api, `Bt Aunt ${stamp}`)).length === 0, "no second own file on the number");
    await page.eval(`${W}.querySelector("[data-close]").click()`);
  } finally {
    await page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED")).catch((e) => { console.error(e.message); process.exit(1); });
}
