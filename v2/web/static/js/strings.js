// The strings scripts need come from the page, translated on the server (design 7.2): see _strings.html.
let table = null;

function load() {
  table = {};
  document.querySelectorAll("#strings [data-k]").forEach((el) => { table[el.dataset.k] = el.textContent; });
}

// t("called", {name: "Mona"}) with "{name}" placeholders in the catalogue.
export function t(key, vars) {
  if (!table) load();
  let text = table[key] != null ? table[key] : key;
  if (vars) for (const [name, value] of Object.entries(vars)) text = text.split(`{${name}}`).join(String(value));
  return text;
}
