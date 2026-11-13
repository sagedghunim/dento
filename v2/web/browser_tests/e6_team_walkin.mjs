// Team & settings and the walk-in (design G1, G5), as someone with team.manage (HAMADA_BT_USER=ahmed after granting
// him team.manage, see README). Switch a whole group for the assistant with a double click (saved once, still there
// after a reload), open a group's single permissions, a dropped connection keeps the switch for "Try again", a
// change from another screen meanwhile answers 409 and the table reads the team again. Then "Walk-in now" from Today
// with a double-clicked submit books once. 390 px without sideways scrolling; an expired session goes to sign-in.
// With HAMADA_BT_SHOTS=<folder> it also takes the 1280 px and 390 px screenshots.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { bookingsOf, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);
const SHOTS = process.env.HAMADA_BT_SHOTS || "";
const GROUP = "visit";  // a group the demo doctor holds whole, with no owner-only permission

async function shot(page, name, full = false) {
  if (!SHOTS) return;
  await page.cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  await sleep(300);
  const r = await page.cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: full });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, "base64"));
  console.log(`  screenshot: ${name}`);
}

const cell = (member, group) => `document.querySelector('button.toggle[data-member="${member}"][data-group="${group}"]')`;
const stateOf = (member, group) => `(() => { const b = ${cell(member, group)}; return b ? b.getAttribute("aria-pressed") : null; })()`;
const toastText = `document.querySelector("#toast.show [data-text]") ? document.querySelector("#toast [data-text]").textContent : ""`;

export async function run() {
  const { session, api } = await signIn();
  const teamNow = async () => (await api("GET", "/api/v1/team")).data;
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/team");
    check(await page.eval(`Boolean(document.querySelector("[data-matrix] table.matrix"))`), "the Who can do what table is drawn");
    let team = await teamNow();
    const assistant = team.members.find((m) => m.role === "assistant" && m.is_active && m.editable);
    check(Boolean(assistant), "an assistant the signed-in person may change");
    check(await page.eval(`document.querySelectorAll("[data-matrix] tbody tr.matrix-group").length === 8`), "eight permission groups");
    check(await page.eval(`${cell(assistant.id, "team")}.disabled && ${cell(assistant.id, "team")}.title.length > 0`),
      "an owner-only group is disabled, with the reason in its title");
    const meCol = team.members.find((m) => m.is_me);
    check(!(await page.eval(`Boolean(${cell(meCol.id, GROUP)})`)) || await page.eval(`${cell(meCol.id, GROUP)}.disabled`),
      "the person's own column can't be switched");
    check(await page.eval(`Boolean(document.querySelector('[data-overview] [data-person="${assistant.id}"]'))`), "Who works where lists the assistant");
    check(await page.eval(`document.querySelector('[data-overview] [data-person="${assistant.id}"]').closest(".person").querySelectorAll(".gchip").length === 8`),
      "with the eight groups for this place");
    await shot(page, "v2-team-1280.png", true);

    // Tapping a person shows only their switches.
    await page.eval(`document.querySelector('[data-overview] [data-person="${assistant.id}"]').click()`);
    await page.until(`document.querySelectorAll("[data-matrix] thead th.matrix-col").length === 1`, 3000, "only that person's column");
    check(await page.eval(`!document.querySelector("[data-person-bar]").hidden`), "their details button shows beside the table");

    // A double click on one switch saves once.
    const before = assistant.groups[GROUP];
    const want = before === "on" ? "false" : "true";
    await page.eval(`(() => { const b = ${cell(assistant.id, GROUP)}; b.click(); b.click(); })()`);
    await page.until(`${stateOf(assistant.id, GROUP)} === "${want}"`, 10000, "the switch shows the new state");
    team = await teamNow();
    let fresh = team.members.find((m) => m.id === assistant.id);
    check(fresh.version === assistant.version + 1, "the double click saved once (version +1)");
    check((fresh.groups[GROUP] === "on") === (want === "true"), "the server holds the new state");
    check((await page.eval(toastText)).length > 0, "a toast says what changed");

    // It is still there after a reload.
    await page.open("/team");
    check((await page.eval(stateOf(assistant.id, GROUP))) === want, "the switch keeps its state after a reload");

    // A row's name opens its single permissions.
    await page.eval(`document.querySelector('[data-expand="${GROUP}"]').click()`);
    await page.until(`document.querySelectorAll('button.toggle[data-member="${assistant.id}"][data-code]').length > 0`, 3000, "the single permissions show");
    check(await page.eval(`document.querySelector('[data-expand="${GROUP}"]').getAttribute("aria-expanded") === "true"`), "the row says it is open");

    // A dropped connection: the switch waits for "Try again", which saves once.
    const versionBefore = fresh.version;
    await page.cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`${cell(assistant.id, GROUP)}.click()`);
    await page.until(`!document.querySelector("[data-try]").hidden`, 40000, "Try again shows after the connection drops");
    check(await page.eval(`document.querySelector("[data-matrix-card] [data-error]").textContent.length > 0`), "it says the save isn't confirmed");
    await page.eval(`${cell(assistant.id, GROUP)}.click()`);  // ignored while unknown
    await page.cdp("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await page.eval(`document.querySelector("[data-try]").click()`);
    await page.until(`document.querySelector("[data-try]").hidden`, 15000, "Try again goes away after the save");
    fresh = (await teamNow()).members.find((m) => m.id === assistant.id);
    check(fresh.version === versionBefore + 1, "the retried switch saved once");
    check((await page.eval(stateOf(assistant.id, GROUP))) === (fresh.groups[GROUP] === "on" ? "true" : fresh.groups[GROUP] === "partial" ? "mixed" : "false"),
      "the table matches the server");

    // Another screen changes the assistant meanwhile: 409, the table reads the team again and says so.
    const other = await api("PUT", `/api/v1/team/members/${assistant.id}/permissions`,
      { version: fresh.version, groups: { [GROUP]: fresh.groups[GROUP] !== "on" } });
    check(other.status === 200, "a change from another screen");
    const serverState = other.data.member.groups[GROUP] === "on" ? "true" : other.data.member.groups[GROUP] === "partial" ? "mixed" : "false";
    await page.eval(`${cell(assistant.id, GROUP)}.click()`);
    await page.until(`${stateOf(assistant.id, GROUP)} === "${serverState}"`, 10000, "the table shows the other screen's change");
    check((await page.eval(toastText)).length > 0, "a toast says someone changed this person meanwhile");
    const after = (await teamNow()).members.find((m) => m.id === assistant.id);
    check(after.version === other.data.member.version, "the stale switch wrote nothing");

    // The member dialog opens with the names.
    await page.eval(`document.querySelector('[data-edit-member="${assistant.id}"]').click()`);
    await page.until(`document.getElementById("member-dialog").open`, 3000, "the member dialog opens");
    check((await page.eval(`document.querySelector('#member-dialog [name="display_name"]').value`)).length > 0, "it shows the name to edit");
    await page.eval(`document.querySelector('#member-dialog [data-close]').click()`);

    // Phones: 390 px, no sideways scrolling.
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await page.open("/team");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "the team page fits 390 px");
    const teamPhoneShot = true;

    // --- Walk-in now, from Today ---
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    await page.open("/");
    const name = `Bt Walk ${stamp}`;
    await page.eval(`[...document.querySelectorAll("[data-open-walkin]")].find((b) => b.offsetParent !== null).click()`);
    await page.until(`document.getElementById("walkin-dialog").open`, 3000, "the walk-in dialog opens");
    await page.until(`document.querySelector('#walkin-dialog [name="doctor"]').value !== ""`, 8000, "the doctor comes from the server's choice");
    await page.eval(`document.querySelector("#walkin-dialog [data-new-patient]").click()`);
    // "Collect now" starts ticked for a walk-in (desk review 2): the fee fills the amount.
    await page.until(`document.querySelector('#walkin-dialog [name="amount"]').value !== ""`, 8000, "the fee fills the amount");
    await page.eval(`(() => { const f = document.querySelector("#walkin-dialog form");
                              f.elements.full_name.value = ${JSON.stringify(name)}; f.elements.phone.value = "0101${String(Date.now()).slice(-7)}"; })()`);
    await shot(page, "v2-walkin-1280.png");
    await page.eval(`(() => { const b = document.querySelector("#walkin-dialog [data-submit]"); b.click(); b.click(); })()`);
    await page.until(`!document.getElementById("walkin-dialog").open`, 10000, "the dialog closes after the walk-in");
    const toast = await page.eval(toastText);
    check(toast.includes(name), `the toast names the turn: "${toast}"`);
    await sleep(500);
    const booked = await bookingsOf(api, name);
    check(booked.length === 1, "the double click booked once");
    check(booked[0].status === "waiting", "the walk-in is already arrived");
    await page.until(`[...document.querySelectorAll(".appt")].some((r) => r.dataset.name === ${JSON.stringify(name)})`, 15000, "the row appears in the queue");

    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await page.open("/");
    check(await page.eval(`document.documentElement.scrollWidth <= 390`), "Today fits 390 px with the walk-in button");
    await page.eval(`[...document.querySelectorAll("[data-open-walkin]")].find((b) => b.offsetParent !== null).click()`);
    await page.until(`document.getElementById("walkin-dialog").open`, 3000, "the walk-in dialog opens at 390 px");
    await page.eval(`document.querySelector('#walkin-dialog [data-close]').click()`);
    // The one phone screenshot, at the end: the team page at 390 px.
    if (teamPhoneShot) { await page.open("/team"); await shot(page, "v2-team-390.png", true); }

    // An expired session: a page load goes to sign-in.
    await page.cdp("Network.deleteCookies", { name: "hamada_session", url: (await page.eval("location.origin")) });
    await page.open("/team");
    check((await page.eval("location.pathname")) === "/login", "an expired session goes to sign-in");
    check(page.errors().length === 0, `no script errors ${page.errors().join(" | ")}`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
