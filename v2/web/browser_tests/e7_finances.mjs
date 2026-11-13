// Finances (design F1, F3, F4, F5), as someone with finances.view, expenses.manage and cash.shift
// (HAMADA_BT_USER=ahmed with finances.view and expenses.manage granted; see README). Add an expense with a double
// click and see the month's expenses rise once; a dropped connection keeps an expense for "Try again"; void it;
// add a category; on the register, "Pay an expense" by its category (double-clicked) takes it from the drawer once and
// lists it by the category's name, "Other" won't save without a reason, and the quiet "other drawer movement" moves the
// expected cash once; every tab fits 390 px; an expired session goes to sign-in. With HAMADA_BT_SHOTS=<folder> it
// takes the register at 1280 and 390 px.
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

const monthExpenses = async (api) => (await api("GET", "/api/v1/finances/profit")).data.expenses;
const expected = async (api) => { const r = (await api("GET", "/api/v1/register")).data; return r.shift ? r.totals.expected_cash : 0; };
const expenseRow = (note) => `[...document.querySelectorAll("[data-expense]")].find((r) => r.textContent.includes(${JSON.stringify(note)}))`;
// Tap a category chip in "Pay an expense": by its code, or the first that isn't "Other". Returns its name.
const pickCategory = (code = "") => `(() => { const chips = [...document.querySelectorAll('#expense-dialog [name="category_id"]')];
  const code = ${JSON.stringify(code)};
  const c = code ? chips.find((i) => i.dataset.code === code) : chips.find((i) => i.dataset.code !== "other");
  c.click(); return c.closest(".chip").textContent.trim(); })()`;
const fill = (dialog, values) => `(() => { const f = document.querySelector("#${dialog} form");
  for (const [k, v] of Object.entries(${JSON.stringify(values)})) f.elements[k].value = v; })()`;

export async function run() {
  const { session, api } = await signIn();
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);

    // --- expenses: a double-clicked add counts once, and the month's total rises by it ---
    const before = await monthExpenses(api);
    await page.open("/finances?tab=expenses");
    check(await page.eval(`document.querySelector('.fin-tab[aria-current="page"]') !== null`), "the finances page opens on the expenses tab");
    const note = `Bt bill ${stamp}`;
    await page.eval(`document.querySelector("[data-new-expense]").click()`);
    await page.until(`document.getElementById("expense-dialog").open`, 3000, "the expense dialog opens");
    check(await page.eval(`!document.querySelector('#expense-dialog [name="from_shift"]').checked`), "on the expenses tab it isn't from the drawer by default");
    await page.eval(pickCategory());
    await page.eval(fill("expense-dialog", { amount: "123.45", note }));
    await page.eval(`(() => { const b = document.querySelector("#expense-dialog [data-submit]"); b.click(); b.click(); })()`);
    await page.until(`Boolean(${expenseRow(note)})`, 10000, "the new expense is listed");
    await sleep(800);
    check(await monthExpenses(api) === before + 12345, "a double click added the expense once (+123.45 on the server)");
    check(await page.eval(`Number(document.querySelector("[data-expenses-total]").dataset.expensesTotal)`) === before + 12345,
      "the month's total on the page rose by 123.45");

    await page.open("/finances?tab=month");
    const statText = await page.eval(`document.querySelector('[data-stat="expenses"]').textContent`);
    check(await monthExpenses(api) === before + 12345 && statText.length > 0, `This month shows the expenses (${statText.trim()})`);

    // --- a dropped connection: the add stays on screen for "Try again", then saves once ---
    await page.open("/finances?tab=expenses");
    const note2 = `Bt lab ${stamp}`;
    await page.eval(`document.querySelector("[data-new-expense]").click()`);
    await page.until(`document.getElementById("expense-dialog").open`, 3000, "the expense dialog opens again");
    await page.eval(pickCategory("lab"));
    await page.eval(fill("expense-dialog", { amount: "10", note: note2 }));
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`document.querySelector("#expense-dialog [data-submit]").click()`);
    await page.until(`document.getElementById("expense-dialog").dataset.unknown === "1"`, 30000, "the expense is marked not confirmed");
    await page.eval(`document.querySelector('#expense-dialog .dialog-head [data-close]').click()`);
    check(await page.eval(`document.getElementById("expense-dialog").open`), "the dialog can't be closed while the outcome is unknown");
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`document.querySelector("#expense-dialog [data-submit]").click()`);
    await page.until(`Boolean(${expenseRow(note2)})`, 15000, "Try again listed the expense");
    await sleep(500);
    check(await monthExpenses(api) === before + 12345 + 1000, "the server has it once (+10)");

    // --- void it: it stays listed, marked, and leaves the total ---
    await page.eval(`${expenseRow(note2)}.querySelector("[data-void-expense]").click()`);
    await page.until(`document.getElementById("void-dialog").open`, 3000, "the void dialog opens");
    await page.eval(`document.querySelector("#void-dialog [data-submit]").click()`);
    await page.until(`${expenseRow(note2)} && ${expenseRow(note2)}.classList.contains("is-void")`, 10000, "the expense shows as voided");
    check(await monthExpenses(api) === before + 12345, "voiding took it out of the month's expenses");

    // --- a new category, offered in the expense dialog ---
    const cat = `Bt cat ${stamp}`;
    await page.eval(`document.querySelector("[data-new-category]").click()`);
    await page.until(`document.getElementById("category-dialog").open`, 3000, "the category dialog opens");
    await page.eval(fill("category-dialog", { name: cat }));
    await page.eval(`document.querySelector("#category-dialog [data-submit]").click()`);
    await page.until(`[...document.querySelectorAll(".cat-list [data-category]")].some((r) => r.textContent.includes(${JSON.stringify(cat)}))`, 10000, "the new category is listed");
    await page.eval(`document.querySelector("[data-new-expense]").click()`);
    await page.until(`document.getElementById("expense-dialog").open`, 3000, "the expense dialog opens");
    check(await page.eval(`[...document.querySelectorAll("[data-category-chips] .chip")].some((o) => o.textContent.trim() === ${JSON.stringify(cat)})`), "the new category is offered for expenses");
    check(await page.eval(`[...document.querySelectorAll('[data-category-chips] [name="category_id"]')].pop().dataset.code === "other"`), "Other is offered last");
    await page.eval(`document.querySelector("#expense-dialog .dialog-head [data-close]").click()`);

    // --- the register: no generic cash in / out; "Pay an expense" by its category, from the drawer ---
    await page.open("/finances?tab=register");
    check(await page.eval(`document.querySelector(".card-sub").textContent.length > 20`), "the register says it opens by itself on the first cash payment");
    check(await page.eval(`!document.querySelector('[data-cash="out"]') && Boolean(document.querySelector("[data-new-expense]"))`), "the register offers Pay an expense, not Cash out");
    const start = await expected(api);
    await page.eval(`document.querySelector("[data-new-expense]").click()`);
    await page.until(`document.getElementById("expense-dialog").open`, 3000, "Pay an expense opens on the register");
    check(await page.eval(`document.querySelector('#expense-dialog [name="from_shift"]').checked`), "from the register it is paid from the drawer by default");
    const labName = await page.eval(pickCategory("lab"));
    check(await page.eval(`document.activeElement === document.querySelector('#expense-dialog [name="amount"]')`), "tapping a category moves to the amount");
    await page.eval(fill("expense-dialog", { amount: "20" }));
    await page.eval(`(() => { const b = document.querySelector("#expense-dialog [data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!document.getElementById("expense-dialog").open`, 10000, "the expense dialog closes");
    await sleep(1000);
    check(await expected(api) === start - 2000, "a double-clicked lab expense took 20 from the drawer once");
    await page.until(`[...document.querySelectorAll("[data-register] .lines li")].some((li) => li.textContent.includes(${JSON.stringify(labName)}))`, 10000, "the drawer list names the expense by its category");

    // "Other" needs a reason: the dialog says so and sends nothing; with one it saves.
    await page.eval(`document.querySelector("[data-new-expense]").click()`);
    await page.until(`document.getElementById("expense-dialog").open`, 3000, "Pay an expense opens again");
    await page.eval(pickCategory("other"));
    check(await page.eval(`!document.querySelector("#expense-dialog [data-note-required]").hidden`), "with Other the note says it is required");
    await page.eval(fill("expense-dialog", { amount: "5" }));
    await page.eval(`document.querySelector("#expense-dialog [data-submit]").click()`);
    await sleep(500);
    check(await page.eval(`document.getElementById("expense-dialog").open && document.querySelector("#expense-dialog [data-error]").textContent.length > 0`), "Other without a reason isn't saved and says why");
    check(await expected(api) === start - 2000, "nothing was taken for Other without a reason");
    await page.eval(fill("expense-dialog", { note: `Bt tip ${stamp}` }));
    await page.eval(`document.querySelector("#expense-dialog [data-submit]").click()`);
    await page.until(`!document.getElementById("expense-dialog").open`, 10000, "Other with a reason saves");
    await sleep(800);
    check(await expected(api) === start - 2500, "Other with a reason took 5");

    // The quiet "other drawer movement": the owner adds change; a reason is required.
    await page.eval(`document.querySelector('[data-cash="in"]').click()`);
    await page.until(`document.getElementById("cash-dialog").open`, 3000, "the other drawer movement opens");
    await page.eval(fill("cash-dialog", { amount: "50", reason: "" }));
    await page.eval(`document.querySelector("#cash-dialog [data-submit]").click()`);
    await sleep(400);
    check(await page.eval(`document.getElementById("cash-dialog").open`), "a movement without a reason isn't sent");
    await page.eval(fill("cash-dialog", { reason: `Bt change ${stamp}` }));
    await page.eval(`(() => { const b = document.querySelector("#cash-dialog [data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!document.getElementById("cash-dialog").open`, 10000, "the cash dialog closes");
    await sleep(1000);
    check(await expected(api) === start + 2500, "a double-clicked movement added 50 once");
    await shot(page, "v2-register-1280.png");
    check(await page.eval(`document.querySelectorAll(".cash-lines li").length >= 2`), "the shift lists its cash lines");
    await page.eval(`document.querySelector("[data-shift-report]").click()`);
    await page.until(`document.querySelector("#report-dialog [data-report-body] .cash-lines")`, 10000, "a shift's report opens");
    await page.eval(`document.querySelector("#report-dialog [data-close]").click()`);
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);

    // --- phones: every tab at 390 px without sideways scrolling ---
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    for (const t of ["month", "register", "expenses", "pay", "owed"]) {
      await page.open(`/finances?tab=${t}`);
      check(await page.eval(`document.documentElement.scrollWidth <= 390`), `the ${t} tab fits 390 px`);
      if (t === "register") await shot(page, "v2-register-390.png", true);
    }

    // An expired session: a page load goes to sign-in.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: (await page.eval("location.origin")) });
    await page.open("/finances");
    check((await page.eval("location.pathname")) === "/login", "an expired session goes to sign-in");
  } finally {
    page.close();
  }
}
if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
