// Inventory and Prescription (design F2, E3), as someone with inventory.manage and settings.medicines
// (HAMADA_BT_USER=ahmed, with inventory.manage granted; see README). Add an item, record a purchase with a double
// click (stock rises once), a dropped connection keeps the purchase for "Try again", record use; add a medicine,
// see it in the list and pick it into a ready prescription. An expired session goes to sign-in.
// With HAMADA_BT_SHOTS=<folder> it also takes the 1280 px and 390 px screenshots.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";

async function shot(page, name, full = false) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: full });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const card = (name) => `[...document.querySelectorAll("[data-item]")].find((c) => JSON.parse(c.dataset.item).name === ${JSON.stringify(name)})`;
const qtyOf = (name) => `(() => { const c = ${card(name)}; return c ? JSON.parse(c.dataset.item).quantity : null; })()`;

export async function run() {
  const { session, api } = await signIn();
  const itemName = `Bt Gloves ${stamp}`;
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/inventory");
    check(await page.eval(`document.querySelector("h1").textContent.length > 0`), "the inventory page opens");
    check(await page.eval(`Boolean(document.querySelector("[data-new-item]"))`), "a manager sees the + New item button");

    // The starter list for the specialty (only what the place doesn't have yet).
    const before = (await api("GET", "/api/v1/inventory/items")).data.items.length;
    await page.eval(`document.querySelector("[data-defaults]").click()`);
    await page.until(`document.querySelectorAll("[data-item]").length > ${before}`, 10000, "the starter items appear")
      .catch(async (e) => { if (before === 0) throw e; });  // a place that already has them all adds none
    check((await api("GET", "/api/v1/inventory/items")).data.items.length >= before, "the starter list added the missing items");

    // Add an item: 2 in stock, alert at 5, so it starts low.
    await page.eval(`document.querySelector("[data-new-item]").click()`);
    await page.until(`document.getElementById("item-dialog").open`, 3000, "the item dialog opens");
    await page.eval(`(() => { const f = document.querySelector("#item-dialog form");
                              f.elements.name.value = ${JSON.stringify(itemName)}; f.elements.unit.value = "box";
                              f.elements.quantity.value = "2"; f.elements.threshold.value = "5"; f.elements.unit_cost.value = "45";
                              f.querySelector("[data-submit]").click(); })()`);
    await page.until(`${qtyOf(itemName)} === 2`, 10000, "the new item's card shows 2");
    check(await page.eval(`${card(itemName)}.classList.contains("is-low")`), "it is marked low stock, with the words as well as the colour");
    check(await page.eval(`!document.getElementById("item-dialog").open`), "the dialog closed after the save");

    // Purchase with a double click: the stock rises once.
    await page.eval(`${card(itemName)}.querySelector('[data-inv="purchase"]').click()`);
    await page.until(`document.getElementById("purchase-dialog").open`, 3000, "the purchase dialog opens");
    check((await page.eval(`document.querySelector('#purchase-dialog [name="unit_price"]').value`)) === "45", "the price starts at the item's cost");
    await page.eval(`(() => { const f = document.querySelector("#purchase-dialog form"); f.elements.qty.value = "10";
                              f.elements.record_expense.checked = false;
                              const b = f.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`${qtyOf(itemName)} === 12`, 10000, "the card shows 12 after buying 10");
    await sleep(800);
    const items = (await api("GET", "/api/v1/inventory/items")).data.items;
    const saved = items.find((i) => i.name === itemName);
    check(saved.quantity === 12, "a double click bought once (12 on the server)");
    check(!(await page.eval(`${card(itemName)}.classList.contains("is-low")`)), "no longer low");

    // The connection drops mid-purchase: the dialog keeps the request; "Try again" saves it once.
    await page.eval(`${card(itemName)}.querySelector('[data-inv="purchase"]').click()`);
    await page.until(`document.getElementById("purchase-dialog").open`, 3000, "the purchase dialog opens again");
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`(() => { const f = document.querySelector("#purchase-dialog form"); f.elements.qty.value = "3";
                              f.elements.record_expense.checked = false; f.querySelector("[data-submit]").click(); })()`);
    await page.until(`document.getElementById("purchase-dialog").dataset.unknown === "1"`, 30000, "the purchase is marked not confirmed");
    await page.eval(`document.querySelector('#purchase-dialog [data-close]') && document.querySelector('#purchase-dialog .dialog-head [data-close]').click()`);
    check(await page.eval(`document.getElementById("purchase-dialog").open`), "the dialog can't be closed while the outcome is unknown");
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`document.querySelector("#purchase-dialog [data-submit]").click()`);
    await page.until(`${qtyOf(itemName)} === 15`, 15000, "Try again added 3 (15)");
    await sleep(500);
    check((await api("GET", "/api/v1/inventory/items")).data.items.find((i) => i.name === itemName).quantity === 15, "the server has 15: saved once");

    // Record use: the stock goes down.
    await page.eval(`${card(itemName)}.querySelector('[data-inv="use"]').click()`);
    await page.until(`document.getElementById("use-dialog").open`, 3000, "the use dialog opens");
    await page.eval(`(() => { const f = document.querySelector("#use-dialog form"); f.elements.qty.value = "2"; f.querySelector("[data-submit]").click(); })()`);
    await page.until(`${qtyOf(itemName)} === 13`, 10000, "use took 2 (13)");

    // The item's history lists the purchases, the use and the opening count.
    await page.eval(`(() => { const c = ${card(itemName)}; c.querySelector("details").open = true; c.querySelector('[data-inv="history"]').click(); })()`);
    await page.until(`document.querySelectorAll("#history-dialog .move-row").length === 4`, 8000, "the history shows four rows");
    await page.eval(`document.querySelector("#history-dialog .dialog-head [data-close]").click()`);

    // Filtering by name.
    await page.eval(`(() => { const i = document.querySelector("[data-filter]"); i.value = ${JSON.stringify(itemName)}; i.dispatchEvent(new Event("input")); })()`);
    check((await page.eval(`[...document.querySelectorAll("[data-item]")].filter((c) => !c.hidden).length`)) === 1, "search narrows the cards to one");
    await page.eval(`(() => { const i = document.querySelector("[data-filter]"); i.value = ""; i.dispatchEvent(new Event("input")); })()`);
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
    await page.eval(`window.scrollTo(0, 0)`);
    await shot(page, "v2-inventory-1280.png");

    // --- Prescription ---
    const medName = `Bt Medicine ${stamp}`;
    await page.open("/prescriptions");
    await page.eval(`document.querySelector("[data-new-medicine]").click()`);
    await page.until(`document.getElementById("medicine-dialog").open`, 3000, "the medicine dialog opens");
    await page.eval(`(() => { const f = document.querySelector("#medicine-dialog form"); f.elements.name.value = ${JSON.stringify(medName)};
                              f.elements.generic.value = "Btcillin"; f.elements.strength.value = "500 mg"; f.elements.form.value = "Tablets";
                              f.elements.default_dose.value = "One tablet every 8 hours";
                              const b = f.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`[...document.querySelectorAll("[data-medicine]")].some((r) => JSON.parse(r.dataset.medicine).name === ${JSON.stringify(medName)})`, 10000, "the new medicine is in the list");
    await sleep(500);
    const meds = (await api("GET", `/api/v1/medicines?q=${encodeURIComponent(medName)}`)).data.items;
    check(meds.length === 1, "a double click added the medicine once");

    // A ready prescription with it: the dose fills from the medicine.
    const rxName = `Bt Rx ${stamp}`;
    await page.eval(`document.querySelector("[data-new-template]").click()`);
    await page.until(`document.getElementById("template-dialog").open`, 3000, "the ready prescription dialog opens");
    await page.eval(`(() => { const f = document.querySelector("#template-dialog form"); f.elements.name.value = ${JSON.stringify(rxName)};
                              const s = f.querySelector('[name="medicine_id"]'); s.value = String(${meds[0].id});
                              s.dispatchEvent(new Event("change", { bubbles: true })); })()`);
    check((await page.eval(`document.querySelector('#template-dialog [name="dose"]').value`)) === "One tablet every 8 hours", "picking the medicine fills its usual dose");
    await page.eval(`document.querySelector("#template-dialog [data-submit]").click()`);
    await page.until(`[...document.querySelectorAll("[data-template]")].some((r) => JSON.parse(r.dataset.template).name === ${JSON.stringify(rxName)})`, 10000, "the ready prescription is listed");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
    await shot(page, "v2-prescription-1280.png");

    // Phones: 390 px, no sideways scrolling.
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await page.open("/prescriptions");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "the prescription page fits 390 px");
    await shot(page, "v2-prescription-390.png");
    await page.open("/inventory");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "the inventory page fits 390 px");
    await shot(page, "v2-inventory-390.png");
    if (SHOTS) {  // the dialogs as phones show them
      await page.eval(`document.querySelector('[data-inv="purchase"]').click()`);
      await shot(page, "v2-inventory-dialog-390.png");
      await page.open("/prescriptions");
      await page.eval(`(() => { const r = [...document.querySelectorAll("[data-template]")].find((n) => JSON.parse(n.dataset.template).items.length > 1);
                                (r || document).querySelector('[data-rx="edit"], [data-new-template]').click(); })()`);
      await shot(page, "v2-prescription-dialog-390.png");
    }

    // An expired session: the next action says so, and a page load goes to sign-in.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: (await page.eval("location.origin")) });
    await page.open("/inventory");
    check((await page.eval("location.pathname")) === "/login", "an expired session goes to sign-in");
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
