// Overview, the owner's home, as someone with reports.view and finances.view (HAMADA_BT_USER=ahmed with both
// granted; see README). Each period opens with no script errors, money shows, the team table lists the doctors,
// and the page fits 390 px. With HAMADA_BT_SHOTS=<folder> it takes v2-overview-1280.png and v2-overview-390.png.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { browser, check, signIn, sleep } from "./lib.mjs";

const SHOTS = process.env.HAMADA_BT_SHOTS || "";

async function shot(page, name, full = false) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: full });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

export async function run() {
  const { session } = await signIn();
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    for (const period of ["today", "week", "month"]) {
      await page.open(`/overview?period=${period}`);
      check(await page.eval(`document.querySelector('.fin-tab[aria-current="page"]').getAttribute("href") === "?period=${period}"`), `${period} opens`);
    }
    check(await page.eval(`Boolean(document.querySelector('[data-ov="revenue"]'))`), "money shows with finances.view");
    check(await page.eval(`document.querySelectorAll(".ov-team tbody tr").length >= 1`), "the team table lists the doctors");
    check(await page.eval(`[...document.querySelectorAll(".nav-btn")].some((a) => a.getAttribute("href") === "/overview")`), "Overview is in the side bar");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
    await shot(page, "v2-overview-1280.png");
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await page.open("/overview?period=month");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "the overview fits 390 px");
    await shot(page, "v2-overview-390.png", true);
  } finally {
    page.close();
  }
}
if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
