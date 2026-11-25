// What the doctor takes from stock in a visit (Saged, 2026-11-25). A tap on the field drops the packages, then the
// items with what is left of each; every letter filters them. A package brings all its items at once. The picks wait
// in a list with their quantities until "Take from stock", which is one request (all or nothing) under one key until
// a definite answer, so a dropped connection never takes stock twice. Stock only: never the bill.
import { errorMessage, isOk, newKey, save } from "./api.js";
import { localDigits, parseWhole } from "./numbers.js";
import { toast } from "./shell.js";
import { t } from "./strings.js";

const data = JSON.parse(document.getElementById("visit-data").textContent);
const stock = data.stock;
const card = document.querySelector("[data-use-card]");
if (stock && card) {
  const input = card.querySelector('[name="use_pick"]');
  const listEl = card.querySelector("#use-list");
  const linesEl = card.querySelector("[data-use-lines]");
  const foot = card.querySelector("[data-use-foot]");
  const errorEl = card.querySelector("[data-use-error]");
  const saveBtn = card.querySelector("[data-use-save]");
  const usedBox = card.querySelector("[data-used-box]");
  const usedEl = card.querySelector("[data-used]");
  const items = new Map(stock.items.map((i) => [i.id, i]));
  const used = new Map(stock.used.map((u) => [u.item_id, u]));
  let sending = null;   // {key, body} until the server gives a definite answer

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

  // --- the list under the field ---
  let shown = [];
  let active = -1;
  function options(typed) {
    const q = plain(typed.trim());
    const found = [];
    for (const kit of stock.kits) {
      const r = rank(plain(kit.name), q);
      if (r >= 0 && kit.lines.some((l) => items.has(l.item_id))) found.push([r, 0, {
        label: kit.name, side: t("use_kit", { n: localDigits(kit.lines.length) }), kit: true, pick: () => addKit(kit) }]);
    }
    for (const item of items.values()) {
      const r = rank(plain(item.name), q);
      if (r >= 0) found.push([r, 1, { label: item.name, side: left(item), low: item.quantity <= 0, pick: () => addItem(item.id, 1) }]);
    }
    return found.sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, , o]) => o);
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
      const li = el("li", o.kit ? "use-kit" : null);
      li.id = `use-list-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      const name = el("bdi", null, o.label);
      name.dir = "auto";
      li.append(name, el("small", o.low ? "use-side use-out" : "use-side", o.side));
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
    if (sending) return;
    o.pick();
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

  // --- the picks, waiting with their quantities ---
  const rowOf = (itemId) => linesEl.querySelector(`[data-item-id="${itemId}"]`);
  function addItem(itemId, qty) {
    const item = items.get(itemId);
    if (!item) return;
    const row = rowOf(itemId);
    if (row) {
      const field = row.querySelector("input");
      field.value = String((parseWhole(field.value) || 0) + qty);
      return;
    }
    const li = el("li", "rx-write-line use-line");
    li.dataset.itemId = String(itemId);
    const name = el("span", "use-name");
    const bdi = el("bdi", null, item.name);
    bdi.dir = "auto";
    name.append(bdi, el("small", "muted", ` · ${item.unit}`));
    const field = el("input");
    field.value = String(qty);
    field.dir = "ltr";
    field.inputMode = "numeric";
    field.autocomplete = "off";
    field.maxLength = 6;
    field.setAttribute("aria-label", `${t("use_qty")}: ${item.name}`);
    const remove = el("button", "icon-btn", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("remove")}: ${item.name}`);
    remove.addEventListener("click", () => { if (!sending) { li.remove(); refresh(); } });
    li.append(name, field, remove);
    linesEl.append(li);
    refresh();
  }
  function addKit(kit) {
    const here = kit.lines.filter((line) => items.has(line.item_id));  // items of another section are not here
    for (const line of here) addItem(line.item_id, line.qty);
    toast(here.length < kit.lines.length
      ? t("use_kit_part", { name: kit.name, n: localDigits(here.length), all: localDigits(kit.lines.length) })
      : t("use_kit_added", { name: kit.name }));
  }
  function refresh() {
    foot.hidden = !linesEl.children.length;
    errorEl.textContent = "";
  }

  // --- what was taken in this visit ---
  function showUsed() {
    usedEl.replaceChildren(...[...used.values()].map((u) => {
      const li = el("li");
      const name = el("bdi", null, u.name);
      name.dir = "auto";
      li.append(name, el("span", "num", `${localDigits(u.qty)} ${u.unit}`));
      return li;
    }));
    usedBox.hidden = !used.size;
  }
  showUsed();

  // --- take from stock ---
  function lock(on) {
    saveBtn.disabled = on;
    input.disabled = on;
    linesEl.querySelectorAll("input, button").forEach((n) => { n.disabled = on; });
  }
  saveBtn.addEventListener("click", async () => {
    if (saveBtn.disabled) return;
    if (!sending) {
      const lines = [];
      for (const li of linesEl.children) {
        const qty = parseWhole(li.querySelector("input").value);
        if (!qty || qty > 100000) { errorEl.textContent = t("bad_qty"); li.querySelector("input").focus(); return; }
        lines.push({ item_id: Number(li.dataset.itemId), qty });
      }
      if (!lines.length) return;
      if (lines.length > 30) { errorEl.textContent = t("use_too_many"); return; }  // the server takes 30 at a time
      sending = { key: newKey(), body: { lines, appointment_id: data.appointment_id } };
    }
    errorEl.textContent = "";
    lock(true);
    const result = await save("POST", "/api/v1/inventory/uses", sending.body, { key: sending.key, label: t("use_label") });
    window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
    if (!result.settled) {  // the answer was lost: the same request again under the same key
      lock(false);
      input.disabled = true;
      linesEl.querySelectorAll("input, button").forEach((n) => { n.disabled = true; });
      errorEl.textContent = t("unknown");
      saveBtn.focus();
      return;
    }
    const body = sending.body;
    sending = null;
    lock(false);
    if (!isOk(result)) {
      const error = result.data && result.data.error;
      if (error && error.code === "out_of_stock" && error.items) {
        errorEl.textContent = t("use_short", { list: error.items.map((i) => `${i.name} (${t("use_left_n", { n: localDigits(i.quantity) })})`).join("، ") });
        for (const i of error.items) { const item = items.get(i.id); if (item) item.quantity = i.quantity; }
      } else errorEl.textContent = errorMessage(result, t("failed"));
      saveBtn.focus();
      return;
    }
    const after = new Map(((result.data && result.data.items) || []).map((i) => [i.id, i.quantity]));
    for (const line of body.lines) {
      const item = items.get(line.item_id);
      if (!item) continue;
      item.quantity = after.has(line.item_id) ? after.get(line.item_id) : item.quantity - line.qty;
      const had = used.get(line.item_id);
      if (had) had.qty += line.qty;
      else used.set(line.item_id, { item_id: line.item_id, name: item.name, unit: item.unit, qty: line.qty });
    }
    linesEl.replaceChildren();
    refresh();
    showUsed();
    input.focus({ preventScroll: true });
    close();
    toast(result.data && result.data.replayed ? t("saved") : t("use_done"));
  });
}
