// Arabic letters typed for a Latin name (Saged, 2026-11-19): "ام" finds Amoxil, "فلاجيل" Flagyl, "بانادول" Panadol.
// Both sides become a rough sound key: consonants by class, vowels dropped (a leading vowel kept as "a"), doubles
// collapsed. Loose on purpose: it only orders suggestions, and the doctor picks one.

const AR = {
  "ب": "b", "پ": "b", "ت": "t", "ط": "t", "ث": "s", "س": "s", "ص": "s", "ز": "s", "ذ": "s", "ظ": "s", "ش": "s",
  "ج": "g", "غ": "g", "ح": "h", "ه": "h", "خ": "k", "ك": "k", "ق": "k", "د": "d", "ض": "d", "ر": "r", "ف": "f",
  "ڤ": "f", "ل": "l", "م": "m", "ن": "n",
};
const AR_VOWEL = /[اأإآعوىيئؤءة]/;

function arabicKey(word) {
  let out = AR_VOWEL.test(word[0] || "") && /[اأإآع]/.test(word[0]) ? "a" : "";
  for (const ch of word) if (AR[ch]) out += AR[ch];
  return out.replace(/(.)\1+/g, "$1");
}

function latinKey(word) {
  let w = word.toLowerCase().replace(/[^a-z]/g, "");
  if (!w) return "";
  const lead = /^[aeiou]/.test(w) ? "a" : "";
  w = w.replace(/^x/, "s").replace(/ph/g, "f").replace(/(th|sh|ch)/g, "s").replace(/kh/g, "k").replace(/gh/g, "g")
    .replace(/ck/g, "k").replace(/c(?=[eiy])/g, "s").replace(/[cq]/g, "k").replace(/x/g, "ks").replace(/z/g, "s")
    .replace(/j/g, "g").replace(/v/g, "f").replace(/p/g, "b").replace(/[aeiouyw]/g, "");
  return (lead + w).replace(/(.)\1+/g, "$1");
}

const HAS_ARABIC = /[\u0600-\u06FF]/;

/** The sound keys of each word of a text, Arabic or Latin. */
export function soundKeys(text) {
  return (text || "").split(/[\s(),/+-]+/).filter(Boolean)
    .map((word) => (HAS_ARABIC.test(word) ? arabicKey(word) : latinKey(word))).filter(Boolean);
}

/** Does Arabic typed text sound like the start of a word of a Latin text? The typed words in order, from any word. */
export function soundsLike(typed, text) {
  if (!HAS_ARABIC.test(typed || "")) return false;
  const q = soundKeys(typed).join("");
  if (q.length < 2) return false;
  const words = soundKeys(text);
  return words.some((_, i) => words.slice(i).join("").startsWith(q));
}
