// One number parser for amounts, quantities, phones and ages (design 6.1, 7.2), the same rules as
// core/numbers.py. Arabic keyboards type Arabic-Indic digits, which Number() and parseFloat() reject.
// Display follows the clinic's digit setting and the page's language, as web/format.py does.

const meta = document.querySelector('meta[name="hamada"]');
const ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩";
const EXTENDED = "۰۱۲۳۴۵۶۷۸۹";

export function asciiDigits(text) {
  return String(text == null ? "" : text).replace(/[٠-٩۰-۹]/g, (ch) => {
    const i = ARABIC_INDIC.indexOf(ch);
    return String(i >= 0 ? i : EXTENDED.indexOf(ch));
  });
}

// A whole number, or null when the text isn't one.
export function parseWhole(text) {
  const t = asciiDigits(text).trim();
  return /^\d+$/.test(t) ? Number(t) : null;
}

// Typed money ("150", "150.5", "١٥٠٫٥") to integer piastres by string arithmetic, or null.
// "1,500" is refused as ambiguous, as on the server.
export function parsePiastres(text) {
  let t = asciiDigits(text).trim();
  if (/^\d+,\d{3}$/.test(t)) return null;
  t = t.replace("٫", ".").replace(",", ".");
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(t);
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] || "").padEnd(2, "0") || "0");
}

const arabicDigits = () => meta && meta.dataset.digits === "arab";

export function localDigits(text) {
  const s = String(text == null ? "" : text);
  return arabicDigits() ? s.replace(/[0-9]/g, (d) => ARABIC_INDIC[Number(d)]) : s;
}

// 125050 -> "1,250.50" (or "١٬٢٥٠٫٥٠"): our own formatter, since Intl's EGP string carries hidden marks.
export function amountText(piastres) {
  const p = Math.round(Number(piastres) || 0);
  const sign = p < 0 ? "-" : "";
  const abs = Math.abs(p);
  const pounds = String(Math.floor(abs / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const rest = abs % 100;
  let text = pounds + (rest ? "." + String(rest).padStart(2, "0") : "");
  if (arabicDigits()) text = localDigits(text.replace(/,/g, "٬").replace(".", "٫"));
  return sign + text;
}

// Money as plain text for messages, with the page's currency pattern: "300 ج.م" or "EGP 300".
export function moneyText(piastres) {
  const holder = document.querySelector('#strings [data-k="money_pattern"]');
  return (holder ? holder.textContent : "EGP %(amount)s").replace("%(amount)s", amountText(piastres));
}

// A whole percent with the sign that goes with the page's digits: "10%" or "١٠٪".
export function percentText(value) {
  return localDigits(value) + (arabicDigits() ? "٪" : "%");
}

// The amount as typed into a field: plain digits, no separators ("1250.5").
export function amountInput(piastres) {
  const p = Math.max(0, Math.round(Number(piastres) || 0));
  const rest = p % 100;
  return String(Math.floor(p / 100)) + (rest ? "." + String(rest).padStart(2, "0") : "");
}

// Money as DOM nodes: the number isolated left to right, the currency placed by the language.
export function moneyNode(piastres, pattern) {
  const span = document.createElement("span");
  span.className = "money";
  const [before, after] = (pattern || "EGP %(amount)s").split("%(amount)s");
  const bdi = document.createElement("bdi");
  bdi.className = "ltr num";
  bdi.textContent = amountText(piastres);
  span.append(before || "", bdi, after || "");
  return span;
}

// The wall-clock part of an ISO time that already carries the clinic's offset: "8:30" and "AM"/"PM".
export function clockParts(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return null;
  const h = Number(m[1]);
  return { time: localDigits(`${h % 12 || 12}:${m[2]}`), pm: h >= 12 };
}

export function phoneText(phone) {
  const p = String(phone || "");
  return localDigits(/^01\d{9}$/.test(p) ? `${p.slice(0, 3)} ${p.slice(3, 7)} ${p.slice(7)}` : p);
}
