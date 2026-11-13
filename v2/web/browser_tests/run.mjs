// Runs every browser test in order: node v2/web/browser_tests/run.mjs  (see README.md)
import { run as rowsStay } from "./e1_rows_stay.mjs";
import { run as noDoubleBooking } from "./e2_no_double_booking.mjs";
import { run as finishCollect } from "./e3_finish_collect.mjs";
import { run as desk } from "./e9_desk.mjs";
import { BASE, USER } from "./lib.mjs";

const tests = [
  ["e1_rows_stay (R8)", rowsStay],
  ["e2_no_double_booking (R7)", noDoubleBooking],
  ["e3_finish_collect (R2)", finishCollect],
  ["e9_desk (desk review)", desk],
];

console.log(`Browser tests against ${BASE} as ${USER}`);
let failed = 0;
for (const [name, test] of tests) {
  console.log(`\n${name}`);
  try {
    await test();
    console.log("  PASSED");
  } catch (error) {
    failed += 1;
    console.error(`  ${error.message}`);
  }
}
console.log(`\n${tests.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
