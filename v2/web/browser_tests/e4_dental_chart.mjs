// The dental chart (design E3), signed in as a doctor (HAMADA_BT_USER=ahmed). Tap a tooth, save a finding with a
// double click (saved once), lose the connection mid-save (the panel keeps the request; "Try again" saves it
// once), record a procedure (one invoice line), add a plan item ("in progress"), and a session that expired.
// With HAMADA_BT_SHOTS=<folder> it also takes the 1280 px and 390 px screenshots.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { browser, check, freeRoom, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";

async function shot(page, name) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

async function inRoom(api, me) {
  const config = (await api("GET", "/api/v1/clinic")).data;
  // The signed-in doctor's own visit: a doctor books only for themselves (Saged, 2026-11-13).
  const doctor = config.doctors.find((d) => String(d.id) === String(me)) || config.doctors.find((d) => d.today.some((s) => s.mode === "queue")) || config.doctors[0];
  const service = config.services.find((s) => s.kind === "procedure").id;
  // A walk-in needs no time, so the test runs for a slot doctor too, at any hour (as e10 does).
  const booked = (await api("POST", "/api/v1/appointments/walk-in", { doctor_id: doctor.id, service_id: service,
                                                              new_patient: { full_name: `Bt Teeth ${stamp}`, phone: `015${String(Date.now() * 1000 + Math.floor(Math.random() * 1000)).slice(-8)}`, sex: "m", age: 30 } })).data.appointment;
  await freeRoom(api, doctor.id);
  await api("POST", `/api/v1/appointments/${booked.id}/call`, { status: "waiting" });
  return booked;
}

const history = async (api, patient, tooth) => (await api("GET", `/api/v1/patients/${patient}/teeth/${tooth}/history`)).data.items.length;

export async function run() {
  const { session, membership, api } = await signIn();
  const visit = await inRoom(api, membership);
  const p = visit.patient.id;
  // Something on the chart for the screenshots: findings, a root canal left open and an extraction.
  for (const [tooth, condition, surfaces] of [[36, "caries", ["O", "D"]], [46, "open_root_canal", []], [18, "extracted", []],
                                              [11, "crown", []], [24, "filling", ["M", "O"]], [48, "missing", []], [37, "implant", []]]) {
    await api("PUT", `/api/v1/patients/${p}/teeth/${tooth}`, { condition, surfaces });
  }

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open(`/visits/${visit.id}/teeth`);
    check((await page.eval(`document.querySelectorAll(".tooth").length`)) === 52, "52 teeth on the page (32 permanent, 20 primary)");
    check((await page.eval(`document.querySelector('.tooth[data-tooth="36"]').dataset.condition`)) === "caries", "tooth 36 shows caries");
    const order = await page.eval(`(() => { const r = (n) => document.querySelector('.tooth[data-tooth="' + n + '"]').getBoundingClientRect().left;
                                     return r(18) < r(11) && r(11) < r(21) && r(21) < r(28) && r(48) < r(41) && r(41) < r(31); })()`);
    check(order, "anatomical order left to right in the right-to-left page");

    // Tap tooth 16: the panel opens with its history.
    await page.eval(`document.querySelector('.tooth[data-tooth="16"]').click()`);
    await page.until(`!document.querySelector("[data-panel]").hidden`, 3000, "the panel opens");
    await page.until(`document.querySelector("[data-history-status]").textContent !== document.querySelector('#strings [data-k="loading"]').textContent`, 8000, "the history loads");

    // A double click saves once.
    const before = await history(api, p, 16);
    await page.eval(`(() => { const f = document.querySelector('[data-part="finding"] form');
                              f.querySelector('[value="caries"]').click(); f.querySelector('[name="surface"][value="O"]').click();
                              const b = f.querySelector("[data-submit]"); b.click(); b.click(); })()`);
    await page.until(`document.querySelector('.tooth[data-tooth="16"]').dataset.condition === "caries"`, 10000, "tooth 16 turns to caries");
    await sleep(500);
    check((await history(api, p, 16)) === before + 1, "a double click wrote one history row");
    check((await page.eval(`document.querySelectorAll("[data-history] li").length`)) === before + 1, "the panel's history shows it, newest first");

    // The connection drops mid-save: the panel keeps the request and offers only Try again or Discard.
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`(() => { const f = document.querySelector('[data-part="finding"] form'); f.querySelector('[value="filling"]').click();
                              f.querySelector("[data-submit]").click(); })()`);
    await page.until(`document.querySelector('[data-part="finding"]').dataset.unknown === "1"`, 30000, "the save is marked not confirmed");
    check(!(await page.eval(`document.querySelector('[data-part="finding"] [data-discard]').hidden`)), "Discard is offered");
    await page.eval(`document.querySelector("[data-panel-close]").click()`);
    check(!(await page.eval(`document.querySelector("[data-panel]").hidden`)), "the panel can't be closed while the save is unknown");
    await page.eval(`document.querySelector('.tooth[data-tooth="17"]').click()`);
    check((await page.eval(`document.querySelector("[data-panel-no]").textContent`)).includes("16") ||
          (await page.eval(`document.querySelector("[data-panel-no]").textContent`)).includes("١٦"), "another tooth can't take over the panel");
    await page.eval(`document.getElementById("alert").click()`);  // the "finish it first" message
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`document.querySelector('[data-part="finding"] [data-submit]').click()`);
    await page.until(`document.querySelector('.tooth[data-tooth="16"]').dataset.condition === "filling"`, 20000, "Try again saves it");
    check((await history(api, p, 16)) === before + 2, "the retried save wrote one row");

    // Record a procedure: one invoice line, and the tooth is marked as having procedures.
    const lines = async () => (await api("GET", `/api/v1/appointments/${visit.id}/record`)).data.procedures.length;
    const linesBefore = await lines();
    await page.eval(`document.querySelector('.tooth[data-tooth="26"]').click()`);
    await page.until(`document.querySelector("[data-panel-no]").textContent.length > 0 && !document.querySelector('[data-part="procedure"]').dataset.busy`, 3000);
    const picked = await page.eval(`(() => { const s = document.querySelector('[data-part="procedure"] select');
      const o = Array.from(s.options).find((x) => x.dataset.condition === "root_canal") || s.options[1]; s.value = o.value;
      s.dispatchEvent(new Event("change", { bubbles: true })); const b = document.querySelector('[data-part="procedure"] [data-submit]'); b.click(); b.click(); return o.textContent; })()`);
    await page.until(`document.querySelector('.tooth[data-tooth="26"]').hasAttribute("data-done")`, 10000, `tooth 26 has a procedure (${picked})`);
    await sleep(500);
    check((await lines()) === linesBefore + 1, "a double click recorded one procedure");

    // Add to the plan: the tooth shows "in progress".
    await page.eval(`(() => { const s = document.querySelector('[data-part="plan"] select'); s.value = s.options[1].value;
                              document.querySelector('[data-part="plan"] [data-submit]').click(); })()`);
    await page.until(`document.querySelector('.tooth[data-tooth="26"]').hasAttribute("data-progress")`, 10000, "tooth 26 is in progress after Add to plan");
    check((await page.eval(`document.querySelectorAll('[data-part="procedure"] .plan-pick').length`)) === 1, "the plan item is offered in Record a procedure");
    check((await page.eval(`document.querySelector('.tooth[data-tooth="46"] .t-rct').offsetParent !== null`)), "tooth 46 shows the RCT badge");

    // Primary teeth.
    await page.eval(`document.querySelector('[name="dentition"][value="primary"]').click()`);
    check((await page.eval(`document.querySelector('[data-dentition="primary"]').hidden === false && document.querySelector('[data-dentition="permanent"]').hidden`)), "the toggle shows the primary teeth");
    await page.eval(`document.querySelector('[name="dentition"][value="permanent"]').click()`);
    await page.eval(`document.querySelector('.tooth[data-tooth="36"]').click()`);
    await sleep(1200);
    await page.eval(`window.scrollTo(0, 0)`);
    await shot(page, "v2-teeth-1280.png");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }

  // At 390 px: the chart scrolls sideways and the panel is a bottom sheet.
  const phone = await browser({ width: 390, height: 844, touch: true });
  try {
    await phone.cookie(session);
    await phone.open(`/visits/${visit.id}/teeth`);
    const scrolls = await phone.eval(`(() => { const s = document.querySelector(".chart-scroll"); return s.scrollWidth > s.clientWidth && document.documentElement.scrollWidth <= 390; })()`);
    check(scrolls, "the chart scrolls inside its card; the page doesn't scroll sideways");
    await phone.eval(`document.querySelector('.tooth[data-tooth="36"]').scrollIntoView({ block: "center", inline: "center" })`);
    await phone.eval(`document.querySelector('.tooth[data-tooth="36"]').click()`);
    await sleep(1200);
    const sheet = await phone.eval(`getComputedStyle(document.querySelector("[data-panel]")).position`);
    check(sheet === "fixed", "the panel is a bottom sheet");
    await phone.eval(`document.querySelector(".chart-card").scrollIntoView({ block: "start" })`);
    await shot(phone, "v2-teeth-390.png");
    check(phone.errors().length === 0, "no script errors at 390 px");
  } finally {
    phone.close();
  }

  // A session that expired: the next save sends the person to sign in, and nothing is lost.
  const late = await browser({ width: 1280, height: 900 });
  try {
    await late.cookie(session);
    await late.open(`/visits/${visit.id}/teeth`);
    await api("POST", "/api/v1/auth/logout", {});
    await late.eval(`document.querySelector('.tooth[data-tooth="15"]').click()`);
    await late.eval(`(() => { const f = document.querySelector('[data-part="finding"] form'); f.querySelector('[value="caries"]').click(); f.querySelector("[data-submit]").click(); })()`);
    await late.until(`location.pathname === "/login"`, 10000, "an expired session goes to sign-in");
    check((await late.eval(`location.search`)).includes("teeth"), "sign-in returns to the chart");
    const pending = await late.eval(`JSON.parse(sessionStorage.getItem("hamada.pending") || "[]").length`);
    check(pending === 1, "the unsent finding waits to be settled after sign-in");
  } finally {
    late.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
