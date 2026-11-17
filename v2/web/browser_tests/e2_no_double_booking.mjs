// M1 decision R7: a booking whose answer was lost is never resent silently, and never done twice.
// The booking reaches the server, but the browser sees a dropped connection (the answer is failed at the
// response stage), so the outcome is unknown. Then:
//   1. the dialog can't be closed (Escape, the close buttons): only "Try again" or "Discard";
//   2. "Try again" sends the same key and body: still one booking;
//   3. a second lost booking, then a page change: the entry is listed in the "not confirmed" bar, not
//      resent, and "Try again" from the bar still leaves one booking.
import { BASE, bookingsOf, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);

export async function run() {
  const { session, api } = await signIn();
  // A queue doctor working today: a slot doctor's booking would need a time, which this test isn't about.
  const config = (await api("GET", "/api/v1/clinic")).data;
  const queueDoctor = config.doctors.find((d) => d.today.some((s) => s.mode === "queue"));
  if (!queueDoctor) throw new Error("This test needs a queue doctor working today.");
  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    let dropAnswers = false;
    page.on(async (m) => {
      if (m.method !== "Fetch.requestPaused") return;
      if (dropAnswers && m.params.request.method === "POST") {
        await page.cdp("Fetch.failRequest", { requestId: m.params.requestId, errorReason: "ConnectionReset" });
      } else {
        await page.cdp("Fetch.continueResponse", { requestId: m.params.requestId });
      }
    });
    await page.cdp("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/appointments", requestStage: "Response" }] });
    await page.open("/");

    async function bookLost(name) {
      dropAnswers = true;
      await page.eval(`(async () => {
        document.querySelector("[data-open-book]").click();
        await new Promise((r) => setTimeout(r, 600));
        const d = document.getElementById("book-dialog");
        d.querySelector("[data-new-patient]").click();
        d.querySelector('[name="full_name"]').value = ${JSON.stringify(name)};
        // A new patient's must-haves (Saged, 2026-11-17): a mobile of their own, the sex and an age.
        d.querySelector('[name="phone"]').value = "015" + String(Date.now()).slice(-8);
        d.querySelector('[name="sex"][value="m"]').checked = true;
        d.querySelector('[name="age"]').value = "30";
        const doctor = d.querySelector('[data-doctors] input[value="${queueDoctor.id}"]');
        doctor.checked = true; doctor.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise((r) => setTimeout(r, 1500));
        const turn = d.querySelector('[name="slot"][value=""]');  // a queue day: "the next turn"
        if (turn) { turn.checked = true; turn.dispatchEvent(new Event("change", { bubbles: true })); }
        d.querySelector("form").requestSubmit();
      })()`);
      await page.until(`document.getElementById("book-dialog").dataset.unknown === "1"`, 40000, "the booking's outcome becomes unknown");
    }

    // --- 1 and 2: the dialog holds the unknown booking; "Try again" doesn't book twice ---
    const first = `Bt Patient ${stamp}a`;
    await bookLost(first);
    check((await bookingsOf(api, first)).length === 1, "the lost booking reached the server once");
    await page.cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await page.cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await sleep(300);
    const state = await page.eval(`(() => { const d = document.getElementById("book-dialog");
      [...d.querySelectorAll("[data-close]")].forEach((b) => b.click());
      return { open: d.open, closeShown: [...d.querySelectorAll("[data-close]")].some((b) => !b.hidden),
               discard: !d.querySelector("[data-discard]").hidden, submit: d.querySelector("[data-submit]").textContent }; })()`);
    check(state.open, "Escape and the close buttons don't close a dialog whose outcome is unknown");
    check(!state.closeShown && state.discard, "only Try again and Discard are offered");
    dropAnswers = false;
    await page.eval(`document.getElementById("book-dialog").querySelector("form").requestSubmit()`);
    await page.until(`!document.getElementById("book-dialog").open`, 15000, "Try again settles the booking");
    check((await bookingsOf(api, first)).length === 1, "Try again with the same key: still one booking");

    // --- 3: a page change never resends it; it waits in the bar ---
    const second = `Bt Patient ${stamp}b`;
    await bookLost(second);
    dropAnswers = false;
    await page.open("/patients");
    await sleep(2500);
    const listed = await page.eval(`({ bar: (document.querySelector("[data-pending-bar]") || {}).textContent || "",
                                       stored: JSON.parse(sessionStorage.getItem("hamada.pending") || "[]").length })`);
    check(listed.stored === 1, "after a page change the unknown booking is still stored, not resent");
    check(listed.bar.includes(second), "the not-confirmed bar names the patient");
    check((await bookingsOf(api, second)).length === 1, "one booking on the server");
    await page.eval(`document.querySelector("[data-pending-bar] .btn").click()`);
    await page.until(`!JSON.parse(sessionStorage.getItem("hamada.pending") || "[]").length`, 15000, "Try again from the bar settles it");
    check((await bookingsOf(api, second)).length === 1, "Try again from the bar: still one booking");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e2_no_double_booking against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
