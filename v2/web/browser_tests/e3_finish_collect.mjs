// M1 decision R2: "collected" only when the answer shows the money. The desk opens "Finish and collect";
// meanwhile the doctor finishes the visit from another screen. The desk's tap gets 409 already_finished:
// the dialog stays open, says nothing was collected, and "Collect now" takes the payment with a new key.
import { BASE, browser, check, signIn, sleep } from "./lib.mjs";

const stamp = Date.now().toString(36).slice(-5);

export async function run() {
  const { session, api } = await signIn();
  const config = (await api("GET", "/api/v1/clinic")).data;
  const doctor = config.doctors.find((d) => d.today.some((s) => s.mode === "queue"));
  if (!doctor) throw new Error("This test needs a queue doctor working today.");
  const service = config.services.find((s) => s.kind === "consultation").id;
  const booked = (await api("POST", "/api/v1/appointments", { doctor_id: doctor.id, service_id: service, arrive: true,
                                                              new_patient: { full_name: `Bt Finish ${stamp}` } })).data.appointment;
  await api("POST", `/api/v1/appointments/${booked.id}/call`, { status: "waiting" });
  const fee = booked.fee;

  const page = await browser({ width: 1280, height: 900 });
  try {
    await page.cookie(session);
    await page.open("/");
    await page.eval(`(() => { const all = document.querySelector('[data-filter-doctor=""]'); if (all) all.click(); })()`);
    await page.until(`document.querySelector('.appt[data-id="${booked.id}"]')?.dataset.status === "in_room"`, 15000, "the visit shows with the doctor");
    await sleep(800);  // past the tap guard
    await page.eval(`document.querySelector('.appt[data-id="${booked.id}"] [data-act="finish"]').click()`);
    await page.until(`document.getElementById("finish-dialog").open`, 5000, "the Finish dialog opens");
    // The doctor finishes the visit on another screen, without a payment.
    check((await api("POST", `/api/v1/appointments/${booked.id}/finish`, { status: "in_room" })).status === 200, "the other screen finished the visit");
    await page.eval(`document.getElementById("finish-form").requestSubmit()`);
    await page.until(`!document.querySelector("#finish-dialog [data-collect-now]").hidden`, 15000, "Collect now is offered");
    const shown = await page.eval(`({ open: document.getElementById("finish-dialog").open, error: document.querySelector("#finish-dialog [data-error]").textContent,
                                      toast: document.querySelector("#toast").classList.contains("show") ? document.querySelector("#toast [data-text]").textContent : "" })`);
    check(shown.open && shown.error.length > 0, `the dialog stays open and says nothing was collected ("${shown.error}")`);
    check(!shown.toast, "no 'collected' toast");
    let visit = (await api("GET", `/api/v1/appointments/${booked.id}`)).data.appointment;
    check(visit.status === "done" && visit.due === fee, "the visit is done and still owes the fee");
    await page.eval(`document.querySelector("#finish-dialog [data-collect-now]").click()`);
    await page.until(`!document.getElementById("finish-dialog").open`, 15000, "Collect now takes the payment");
    visit = (await api("GET", `/api/v1/appointments/${booked.id}`)).data.appointment;
    check(visit.due === 0 && visit.invoice.paid === fee, "the payment was recorded once");
    check(page.errors().length === 0, `no script errors (${page.errors().join(" | ")})`);
  } finally {
    page.close();
  }
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}`) {
  console.log(`e3_finish_collect against ${BASE}`);
  run().then(() => console.log("PASSED"), (error) => { console.error(error.message); process.exit(1); });
}
