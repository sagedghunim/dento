// What the visit uses from stock (Saged, 2026-11-25). A recorded procedure brings its materials in as lines; every
// line has a quantity that goes up or down and an X; the field under them drops the section's items with what is left,
// and every letter filters them. Nothing is sent from here: "Done with this visit" (room.js) reads the lines through
// card.useLines() and the finish takes them off the stock in its own transaction. Until then the list is kept in this
// tab, so a reload loses nothing. Stock only: never the bill.
import { localDigits, parseWhole } from "./numbers.js";
import { t } from "./strings.js";

const data = JSON.parse(document.getElementById("visit-data").textContent);
const stock = data.stock;
const card = document.querySelector("[data-use-card]");
if (stock && card) {
  const input = card.querySelector('[name="use_pick"]');
  const listEl = card.querySelector("#use-list");
  const linesEl = card.querySelector("[data-use-lines]");
  const errorEl = card.querySelector("[data-use-error]");
  const usedBox = card.querySelector("[data-used-box]");
  const usedEl = card.querySelector("[data-used]");
  const items = new Map(stock.items.map((i) => [i.id, i]));
  const recipes = stock.recipes || {};
  const draftKey = `hamada.uses.${data.appointment_id}`;
  let counted = new Set();   // the records whose materials are already in the list

  const plain = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
    .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function rank(text, q) {
    if (!q) return 0;
    if (!text.includes(q)) return -1;
    if (text.startsWith(q)) return 0;
    if (text.split(/[\s(),/%-]+/).some((w) => w.startsWith(q))) return 1;
    return q.length === 1 ? -1 : 2;  // one letter: names or words that start with it
  }
  const left = (item) => t("use_left", { n: localDigits(item.quantity), unit: item.unit });

  // --- the lines ---
  const rowOf = (itemId) => linesEl.querySelector(`[data-item-id="${itemId}"]`);
  const qtyOf = (row) => parseWhole(row.querySelector("input").value) || 0;
  function setQty(itemId, qty) {
    const item = items.get(itemId);
    if (!item) return;  // another section's item, or one no longer used
    let row = rowOf(itemId);
    if (qty <= 0) { if (row) row.remove(); return; }
    if (!row) {
      row = el("li", "rx-write-line use-line");
      row.dataset.itemId = String(itemId);
      const name = el("span", "use-name");
      const bdi = el("bdi", null, item.name);
      bdi.dir = "auto";
      name.append(bdi, el("small", "muted", ` · ${item.unit} · ${left(item)}`));
      const field = el("input");
      field.dir = "ltr";
      field.inputMode = "numeric";
      field.autocomplete = "off";
      field.maxLength = 6;
      field.setAttribute("aria-label", `${t("use_qty")}: ${item.name}`);
      const remove = el("button", "icon-btn", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `${t("remove")}: ${item.name}`);
      remove.addEventListener("click", () => {
        const next = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        keep();
        (next ? next.querySelector("input") : input).focus();  // the focus stays in the card
      });
      row.append(name, field, remove);
      linesEl.append(row);
    }
    row.querySelector("input").value = String(qty);
  }
  const addQty = (itemId, qty) => setQty(itemId, (rowOf(itemId) ? qtyOf(rowOf(itemId)) : 0) + qty);
  function addRecord(record) {
    if (counted.has(record.id)) return;
    counted.add(record.id);
    for (const line of recipes[record.procedure_id] || []) addQty(line.item_id, line.qty * record.qty);
  }
  linesEl.addEventListener("input", keep);

  // --- kept in this tab until the finish ---
  function keep() {
    errorEl.textContent = "";
    try {
      sessionStorage.setItem(draftKey, JSON.stringify({
        lines: [...linesEl.children].map((li) => [Number(li.dataset.itemId), li.querySelector("input").value]),
        counted: [...counted], used: usedMark }));
    } catch (e) { /* private mode: the list lives as long as the page */ }
  }
  // A saved list is from before a finish when stock was taken since, or a procedure it counted is no longer
  // pending (taken or removed): then it is dropped and the list is built again from what is pending.
  const usedMark = JSON.stringify(stock.used.map((u) => [u.item_id, u.qty]));
  let draft = null;
  try { draft = JSON.parse(sessionStorage.getItem(draftKey) || "null"); } catch (e) { draft = null; }
  if (draft && (draft.used !== usedMark || !draft.counted.every((id) => stock.pending.some((r) => r.id === id)))) draft = null;
  if (draft) {
    for (const [itemId, qty] of draft.lines) {
      if (items.has(itemId)) { setQty(itemId, 1); rowOf(itemId).querySelector("input").value = qty; }
    }
    counted = new Set(draft.counted);
  }
  stock.pending.forEach(addRecord);
  keep();

  window.addEventListener("hamada:use-add", (event) => {
    const d = event.detail;
    addRecord({ id: d.record, procedure_id: d.procedure_id, qty: d.qty });
    keep();
  });
  window.addEventListener("hamada:use-drop", (event) => {
    // A procedure taken off: what it brought in goes down again (to nothing when nobody changed it).
    const id = event.detail.record;
    if (!counted.has(id)) return;
    counted.delete(id);
    const record = stock.pending.find((r) => r.id === id) || event.detail;
    const lines = recipes[record.procedure_id];
    if (lines) for (const line of lines) if (rowOf(line.item_id)) addQty(line.item_id, -line.qty * (record.qty || 1));
    keep();
  });
  window.addEventListener("hamada:use-add", (event) => {  // remembered for a removal later
    const d = event.detail;
    if (!stock.pending.some((r) => r.id === d.record)) stock.pending.push({ id: d.record, procedure_id: d.procedure_id, qty: d.qty });
  });

  // Read by "Done with this visit": {lines} or {error}. A zero or an empty quantity drops the line.
  card.useLines = () => {
    const lines = [];
    for (const li of linesEl.children) {
      const raw = li.querySelector("input").value.trim();
      const qty = raw === "" ? 0 : parseWhole(raw);
      if (qty === null || qty > 1000000) {
        errorEl.textContent = t("bad_qty");
        li.querySelector("input").focus();
        return { error: t("bad_qty") };
      }
      if (qty) lines.push({ item_id: Number(li.dataset.itemId), qty });
    }
    if (lines.length > 30) { errorEl.textContent = t("use_too_many"); return { error: t("use_too_many") }; }
    return { lines, records: [...counted] };  // a procedure recorded since (another tab): its recipe as it is
  };
  card.clearUses = () => { try { sessionStorage.removeItem(draftKey); } catch (e) { /* none */ } };

  // --- the list under the field ---
  let shown = [];
  let active = -1;
  function options(typed) {
    const q = plain(typed.trim());
    const found = [];
    for (const item of items.values()) {
      const r = rank(plain(item.name), q);
      if (r >= 0) found.push([r, { item, low: item.quantity <= 0 }]);
    }
    return found.sort((a, b) => a[0] - b[0]).slice(0, 60).map(([, o]) => o);  // a few hundred items: keep typing
  }
  const close = () => {
    listEl.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const mark = () => {
    [...listEl.children].forEach((li, i) => {
      li.classList.toggle("active", i === active);
      if (li.id) li.setAttribute("aria-selected", String(i === active));
    });
    const li = listEl.children[active];
    if (li) { input.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
    else input.removeAttribute("aria-activedescendant");
  };
  function open() {
    shown = options(input.value);
    active = -1;
    listEl.replaceChildren(...shown.map((o, i) => {
      const li = el("li");
      li.id = `use-list-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      const name = el("bdi", null, o.item.name);
      name.dir = "auto";
      li.append(name, el("small", o.low ? "use-side use-out" : "use-side", left(o.item)));
      li.addEventListener("mousedown", (event) => event.preventDefault());  // the field keeps the focus
      li.addEventListener("click", () => choose(o));
      return li;
    }));
    if (!shown.length && input.value.trim()) {
      const none = el("li", "use-none muted", t("use_none"));
      none.setAttribute("role", "option");
      none.setAttribute("aria-disabled", "true");
      listEl.append(none);
    }
    listEl.hidden = !listEl.children.length;
    input.setAttribute("aria-expanded", String(!listEl.hidden));
  }
  function choose(o) {
    addQty(o.item.id, 1);
    keep();
    input.value = "";
    close();
  }
  listEl.addEventListener("mousedown", (event) => event.preventDefault());  // its scrollbar keeps the list open
  input.addEventListener("focus", open);
  input.addEventListener("click", () => { if (listEl.hidden) open(); });
  input.addEventListener("input", open);
  input.addEventListener("blur", close);
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (listEl.hidden) { open(); return; }
      if (!shown.length) return;
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      mark();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (active >= 0 && !listEl.hidden) choose(shown[active]);
      else if (shown.length === 1) choose(shown[0]);
    } else if (event.key === "Escape" && !listEl.hidden) {
      event.preventDefault();
      close();
    }
  });

  // --- what this visit already took (a visit the doctor went back into) ---
  usedEl.replaceChildren(...stock.used.map((u) => {
    const li = el("li");
    const name = el("bdi", null, u.name);
    name.dir = "auto";
    li.append(name, el("span", "num", `${localDigits(u.qty)} ${u.unit}`));
    return li;
  }));
  usedBox.hidden = !stock.used.length;
}
