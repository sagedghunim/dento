// The receipt page (design 7.5): letterhead offsets, the 80 mm page height, and printing after the fonts load.
const body = document.body;
const receipt = document.querySelector(".receipt");
const top = Number(body.dataset.topMm) || 0;
const bottom = Number(body.dataset.bottomMm) || 0;
if (top) receipt.style.paddingTop = `${top}mm`;  // el.style works under the CSP; style attributes don't
if (bottom) receipt.style.paddingBottom = `${bottom}mm`;

function sizeRoll() {
  if (body.dataset.paper !== "80mm") return;
  const mm = Math.ceil((receipt.getBoundingClientRect().height * 25.4) / 96) + 4;
  const sheet = [...document.styleSheets].find((s) => (s.href || "").includes("print-80mm"));
  try { sheet.insertRule(`@page { size: 80mm ${mm}mm; margin: 0; }`, sheet.cssRules.length); } catch (e) { /* the browser's size */ }
  body.dataset.pageMm = String(mm);
}

async function ready() {
  try { await document.fonts.ready; } catch (e) { /* older browser */ }
  sizeRoll();
}

const prepared = ready();
document.querySelector("[data-print]").addEventListener("click", async () => { await prepared; window.print(); });
if (body.dataset.autoPrint) prepared.then(() => setTimeout(() => window.print(), 100));
