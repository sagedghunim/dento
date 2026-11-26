// Purchase orders and suppliers (design H1). Low stock prepares a draft per supplier; the desk checks it, sends it on
// WhatsApp and receives what came. Each save goes through a dialog's Submitter (one request key until a definite
// answer); buttons stay disabled while a request runs and an unconfirmed one offers only "Try again" or "Discard".
// The server checks every permission again: the buttons here only follow what the page was told.
import { errorMessage, get, isOk, newKey, save } from "./api.js";
import { Submitter, clockNode, el, openDialog, wireClose } from "./dialogs.js";
import { amountInput, localDigits, moneyText, parsePiastres, parseWhole, phoneText } from "./numbers.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const readJson = (id) => { const node = document.getElementById(id); return node ? JSON.parse(node.textContent) : null; };
const data = readJson("order-data") || { items: [], orders: [], perms: {} };
const perms = data.perms || {};
const items = data.items || [];
let suppliers = readJson("suppliers-data") || [];
let ordersState = "open";

const ordersList = document.querySelector("[data-orders]");
const ordersStatus = document.querySelector("[data-orders-status]");
const suppliersList = document.querySelector("[data-suppliers]");
const suppliersStatus = document.querySelector("[data-suppliers-status]");

const STATUS = { draft: ["amber", "ord_status_draft"], sent: ["teal", "ord_status_sent"], done: ["green", "ord_status_done"],
                 cancelled: ["grey", "ord_status_cancelled"] };

function pill(node, status) {
  const [colour, key] = STATUS[status] || ["grey", status];
  node.className = `pill ${colour}`;
  node.textContent = t(key);
}

function dayText(iso) {
  if (!iso) return "";
  const date = new Date(/T/.test(iso) ? iso : `${iso}T12:00:00`);
  if (Number.isNaN(date.getTime())) return "";
  const lang = document.documentElement.lang || "ar";
  const options = { day: "numeric", month: "long", numberingSystem: "latn" };
  if (/T/.test(iso) && data.zone) options.timeZone = data.zone;  // the clinic's day, not the browser's
  try { return localDigits(new Intl.DateTimeFormat(lang, options).format(date)); }
  catch (e) { delete options.timeZone; return localDigits(new Intl.DateTimeFormat(lang, options).format(date)); }
}

const bdi = (text, className) => { const node = el("bdi", className, text); node.dir = "auto"; return node; };
const button = (text, attrs, className = "btn ghost") => {
  const node = el("button", className, text);
  node.type = "button";
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

// --- the side bar's count (design H1 flow 2) ---

function showDrafts(n) {
  const link = document.querySelector('[data-nav-part][href$="part=orders"]');
  if (link) {
    if (!link.dataset.base) link.dataset.base = link.textContent.replace(/\s*\(.*\)\s*$/, "");
    link.textContent = n ? `${link.dataset.base} (${localDigits(n)})` : link.dataset.base;
  }
  const group = link && link.closest("[data-nav-group]");
  const summary = group && group.querySelector("summary");
  if (!summary) return;
  let count = summary.querySelector(".nav-count");
  if (!n) { if (count) count.remove(); return; }
  if (!count) { count = el("span", "nav-count num"); summary.append(" ", count); }
  count.textContent = localDigits(n);
}

// --- the orders list ---

function renderOrders(list, more = false) {
  if (!ordersList) return;
  if (!more) ordersList.replaceChildren();
  if (!list.length && !more) {
    ordersList.append(el("li", "empty-note", t(ordersState === "open" ? "ord_none_open" : "ord_none_closed")));
    return;
  }
  for (const order of list) {
    const li = el("li");
    const main = el("div", "mat-main");
    const head = el("strong");
    head.append(bdi(order.supplier_name));
    const status = el("span");
    pill(status, order.status);
    main.append(head, " ", status);
    const when = order.status === "sent" ? t("ord_sent_on", { day: dayText(order.sent_at) })
      : order.closed_at ? dayText(order.closed_at) : t("ord_started_on", { day: dayText(order.created_at) });
    main.append(el("p", "muted", `${t("ord_lines", { n: localDigits(order.lines) })} · ${when}`));
    const action = order.status === "draft" ? "ord_check_send" : order.status === "sent" ? "ord_open_receive" : "ord_open";
    li.append(main, button(t(action), { "data-open-order": String(order.id) }));
    ordersList.append(li);
  }
}

// Received or cancelled orders come 50 at a time; "Load more" asks for the next ones.
const ordersFoot = document.querySelector("[data-orders-foot]");
const moreButton = document.querySelector("[data-orders-more]");
let ordersNext = null;
let ordersTicket = 0;

async function loadOrders(more = false) {
  if (!ordersList) return;
  const ticket = ++ordersTicket;
  const needsAt = needsTicket;
  ordersStatus.textContent = t("loading");
  if (moreButton) moreButton.disabled = true;
  const after = more && ordersNext ? `&after=${encodeURIComponent(ordersNext)}` : "";
  const result = await get(`/api/v1/inventory/orders?state=${ordersState}${after}`);
  if (ticket !== ordersTicket) return;  // a newer load won
  if (moreButton) moreButton.disabled = false;
  if (!isOk(result)) { ordersStatus.textContent = errorMessage(result, t("failed")); return; }
  ordersStatus.textContent = "";
  renderOrders(result.data.orders, more);
  ordersNext = result.data.next || null;
  if (ordersFoot) ordersFoot.hidden = !ordersNext;
  showDrafts((result.data.drafts || 0) + (result.data.needs || 0));
  // The "need a supplier" box comes with the orders; one changed here since this load was asked keeps its rows.
  if (Array.isArray(result.data.need_items) && needsAt === needsTicket) renderNeeds(result.data.need_items);
}
if (moreButton) moreButton.addEventListener("click", () => { if (!moreButton.disabled) loadOrders(true); });

document.querySelectorAll("[data-orders-state]").forEach((chip) => chip.addEventListener("click", () => {
  ordersState = chip.dataset.ordersState;
  document.querySelectorAll("[data-orders-state]").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
  loadOrders().catch(() => {});
}));

// "Order all that's short": every low item with a supplier goes on its supplier's draft.
const refillButton = document.querySelector("[data-refill]");
if (refillButton) refillButton.addEventListener("click", async () => {
  if (refillButton.disabled) return;
  refillButton.disabled = true;
  const result = await save("POST", "/api/v1/inventory/orders/refill", {}, { key: newKey(), label: refillButton.textContent.trim() });
  refillButton.disabled = false;
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { alertMessage(t("ord_not_sure")); loadOrders(); return; }
  if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
  const n = (result.data.touched || []).length;
  toast(n ? t("ord_refilled", { n: localDigits(n) }) : t("ord_refill_none"));
  ordersState = "open";
  document.querySelectorAll("[data-orders-state]").forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.ordersState === "open")));
  loadOrders().catch(() => {});
});

// --- a pick list that drops under the field and filters on each letter (Saged, 2026-11-25) ---

const plain = (text) => (text || "").toLocaleLowerCase().replace(/[ً-ْـ]/g, "")
  .replace(/[أإآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");

// onPick may answer the text to leave in the field; onNew (optional) adds "+ New: <typed text>" at the end.
// opts.top() gives lines shown first while nothing is typed ({name, run}); opts.quiet closes the list when
// nothing matches; opts.noFocus opens it on a tap or a letter, not on focus. An item's .tag is a small note.
function picker(input, list, source, onPick, onNew = null, opts = {}) {
  let shown = [];
  let active = -1;
  const mark = () => {
    [...list.children].forEach((li, i) => { li.classList.toggle("active", i === active); li.setAttribute("aria-selected", String(i === active)); });
    const li = list.children[active];
    if (li && li.id) { input.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
    else input.removeAttribute("aria-activedescendant");
  };
  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
  const choose = (item) => {
    const after = item.run ? item.run() : item.isNew ? onNew(item.name) : onPick(item);
    input.value = typeof after === "string" ? after : "";
    close();
  };
  const open = () => {
    const q = plain(input.value.trim());
    shown = source().filter((item) => !q || plain(item.name).includes(q))
      .sort((a, b) => (q && plain(a.name).startsWith(q) ? 0 : 1) - (q && plain(b.name).startsWith(q) ? 0 : 1)).slice(0, 40);
    const typed = input.value.trim();
    if (onNew && typed && !shown.some((item) => plain(item.name) === q)) shown.push({ isNew: true, name: typed });
    if (!q && opts.top) shown.unshift(...opts.top());
    if (!shown.length && opts.quiet) { close(); return; }
    list.replaceChildren(...shown.map((item, i) => {
      const li = el("li");
      li.id = `${list.id}-${i}`;
      li.setAttribute("role", "option");
      if (item.isNew) li.append(t("ord_add_new"), " ", bdi(item.name));
      else if (item.run) { li.append(el("strong", "", item.name)); li.classList.add("on"); }
      else li.append(bdi(item.name));
      if (item.tag) li.append(el("small", "muted", item.tag));
      if (item.quantity != null) li.append(el("small", "muted", t("ord_have", { n: localDigits(item.quantity), unit: item.unit || "" })));
      li.addEventListener("mousedown", (e) => e.preventDefault());
      li.addEventListener("click", () => choose(item));
      return li;
    }));
    if (!shown.length) {
      const none = el("li", "muted", t("ord_no_item"));
      none.setAttribute("aria-disabled", "true");
      list.append(none);
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    active = -1;
    mark();
  };
  if (!opts.noFocus) input.addEventListener("focus", open);
  input.addEventListener("click", () => { if (list.hidden) open(); });
  input.addEventListener("input", open);
  input.addEventListener("blur", close);
  list.addEventListener("mousedown", (event) => event.preventDefault());  // its scrollbar keeps the list open
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) { open(); return; }
      if (!shown.length) return;
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      mark();
    } else if (event.key === "Enter") {
      event.preventDefault();  // never submits the form from the pick list
      if (!list.hidden && shown[active]) choose(shown[active]);
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  });
  return { close };
}

// Two taps for what can't be undone: the first changes the words for a few seconds.
function twoTaps(node) {
  // A button with its own aria-label says "tap again" there too, or a screen reader never hears it.
  const reset = () => {
    delete node.dataset.armed;
    node.textContent = node.dataset.label;
    if (node.dataset.aria) { node.setAttribute("aria-label", node.dataset.aria); delete node.dataset.aria; }
  };
  if (node.dataset.armed) { reset(); return true; }
  node.dataset.label = node.textContent;
  node.dataset.armed = "1";
  node.textContent = t("ord_tap_again");
  if (node.hasAttribute("aria-label")) { node.dataset.aria = node.getAttribute("aria-label"); node.setAttribute("aria-label", t("ord_tap_again")); }
  setTimeout(() => { if (node.dataset.armed) reset(); }, 4000);
  return false;
}

// Where short quantities went (design H1 backup): the backup supplier's order, or the "need a supplier" list.
// A supplier's items for the pick lists: theirs first with a small mark, then the rest (Saged, 2026-11-26).
const ownFirst = (supplierId) => items.map((i) => (supplierId && i.supplier_id === supplierId ? { ...i, tag: t("ord_from_this") } : i))
  .sort((a, b) => (a.tag ? 0 : 1) - (b.tag ? 0 : 1));
// The refill qty, as the server's refill counts it: up to the most, or twice the alert line; at least 1.
const refillQty = (i) => Math.max((i.max_qty > 0 ? i.max_qty : 2 * i.threshold) - i.quantity, 1);
// As the server's is_mobile (inventory/orders.py): 010, 011, 012 or 015 and 8 more digits; Arabic digits too.
function isMobile(value) {
  let digits = [...(value || "")].map((c) => { const n = "٠١٢٣٤٥٦٧٨٩".indexOf(c); const m = "۰۱۲۳۴۵۶۷۸۹".indexOf(c);
    return n >= 0 ? String(n) : m >= 0 ? String(m) : /[0-9]/.test(c) ? c : ""; }).join("");
  if (digits.length === 11 && digits.startsWith("01")) digits = "2" + digits;
  return /^201[0125]\d{8}$/.test(digits);
}
const twoWords = (name) => name.trim().split(/\s+/).filter(Boolean).length >= 2;

const MAX_LINES = 30;  // as the server takes in one order (inventory/views.py)
// The two lines on top: all that is short from this supplier, or all of its items; never one already there.
function supplierLines(supplierId, there, add) {
  if (!supplierId) return [];
  const own = items.filter((i) => i.supplier_id === supplierId && !there.has(i.id));
  const short = own.filter((i) => i.quantity <= i.threshold && !(i.need_qty > 0));  // as the server's refill
  const out = [];
  if (short.length) out.push({ name: t("ord_add_short", { n: localDigits(String(short.length)) }), run: () => add(short.map((i) => [i, refillQty(i)])) });
  if (own.length > short.length) {
    out.push({ name: t("ord_add_all", { n: localDigits(String(own.length)) }), run: () => add(own.map((i) => [i, 1])) });
  }
  return out;
}

const itemName = (id, fallback = "") => (items.find((i) => i.id === id) || {}).name || fallback || t("ord_an_item");
function movedText(moved, fallback = "") {
  return (moved || []).map((m) => (m.to === "supplier"
    ? t("ord_moved_to", { item: itemName(m.item_id, fallback), n: localDigits(m.qty), name: m.supplier_name })
    : t("ord_moved_list", { item: itemName(m.item_id, fallback), n: localDigits(m.qty) }))).join(" · ");
}

// --- one order (a draft is checked and sent here; a sent one received) ---

const orderDialog = document.getElementById("order-dialog");
let current = null;     // the order as the server last answered
let shownAs = "";       // its lines and note when shown, to know what changed
let openOrder = async () => {};

if (orderDialog) {
  const form = orderDialog.querySelector("form");
  const submitter = new Submitter(orderDialog);
  const lines = orderDialog.querySelector("[data-order-lines]");
  const addBox = orderDialog.querySelector("[data-order-add]");
  const noteWrap = orderDialog.querySelector("[data-order-note-wrap]");
  const noteField = form.elements.namedItem("note");
  const noteText = orderDialog.querySelector("[data-order-note]");
  const waBox = orderDialog.querySelector("[data-wa-box]");
  const waLink = orderDialog.querySelector("[data-wa-link]");
  const submit = orderDialog.querySelector("[data-submit]");
  const cancelButton = orderDialog.querySelector("[data-cancel-order]");
  const saveButton = orderDialog.querySelector("[data-save-order]");
  let warned = false;

  const draftNow = () => ({
    lines: [...lines.querySelectorAll("[data-line]")].map((li) => ({
      item_id: Number(li.dataset.line), qty: parseWhole(li.querySelector("input").value) })),
    note: noteField.value.trim(),
  });
  const changed = () => current && current.status === "draft" && JSON.stringify(draftNow()) !== shownAs;

  // Leaving a draft with changes not saved: the first close says so; the second closes.
  const holdClose = (event) => {
    if (!changed() || warned || orderDialog.dataset.busy || orderDialog.dataset.unknown) return;
    warned = true;
    event.preventDefault();
    event.stopImmediatePropagation();
    submitter.say(t("ord_unsaved"));
  };
  orderDialog.addEventListener("click", (event) => { if (event.target.closest("[data-close]")) holdClose(event); }, true);
  orderDialog.addEventListener("cancel", holdClose);
  wireClose(orderDialog);

  function lineNode(line, draft) {
    const li = el("li");
    const main = el("div", "mat-main");
    main.append(el("strong")); main.firstChild.append(bdi(line.name));
    if (draft) {
      li.dataset.line = String(line.item_id);
      main.append(el("p", "muted", t("ord_have", { n: localDigits(line.quantity), unit: line.unit || "" })));
      const qty = el("input", "qty-input");
      Object.assign(qty, { dir: "ltr", inputMode: "numeric", autocomplete: "off", maxLength: 7, value: String(line.qty) });
      qty.setAttribute("aria-label", t("ord_qty_of", { name: line.name }));
      const unit = el("span", "muted", line.unit || "");
      const remove = button(t("ord_remove"), { "data-remove-line": "", "aria-label": t("ord_remove_of", { name: line.name }) });
      const side = el("div", "row-actions");
      side.append(qty, unit, remove);
      li.append(main, side);
    } else {
      const got = line.received ? t("ord_got", { n: localDigits(line.received) }) : "";
      const short = line.missing ? t("ord_missing_n", { n: localDigits(line.missing) }) : "";
      main.append(el("p", "muted", [t("ord_asked", { n: localDigits(line.qty), unit: line.unit || "" }), got, short].filter(Boolean).join(" · ")));
      li.append(main);
      // "He doesn't have it": what is still to come moves to the backup supplier or the list.
      const left = line.qty - (line.received || 0) - (line.missing || 0);
      if (current && current.status === "sent" && left > 0) {
        li.append(button(t("ord_not_here"), { "data-not-here": String(line.item_id), "data-name": line.name,
                                             "aria-label": t("ord_not_here_of", { name: line.name }) }));
      }
    }
    return li;
  }

  function render(order) {
    current = order;
    warned = false;
    const draft = order.status === "draft";
    orderDialog.querySelector("[data-order-supplier]").textContent = order.supplier_name;
    pill(orderDialog.querySelector("[data-order-status]"), order.status);
    const meta = [order.supplier_phone ? phoneText(order.supplier_phone) : t("ord_no_phone")];
    if (order.sent_at) meta.push(t("ord_sent_on", { day: dayText(order.sent_at) }));
    orderDialog.querySelector("[data-order-meta]").textContent = meta.join(" · ");
    lines.replaceChildren(...order.lines.map((line) => lineNode(line, draft)));
    if (!order.lines.length) lines.append(el("li", "empty-note", t("ord_empty")));
    addBox.hidden = !draft;
    noteWrap.hidden = !draft;
    noteField.value = order.note || "";
    noteText.hidden = draft || !order.note;
    noteText.textContent = order.note || "";
    saveButton.hidden = !draft;
    cancelButton.hidden = !["draft", "sent"].includes(order.status);
    cancelButton.textContent = t("ord_cancel");
    delete cancelButton.dataset.armed;
    submit.hidden = !["draft", "sent"].includes(order.status);
    if (!submitter.unknown) submit.textContent = t(draft ? "ord_send" : "ord_receive");
    shownAs = draft ? JSON.stringify(draftNow()) : "";
  }

  const addLine = (item, qty = 1, many = false) => {
    const there = lines.querySelector(`[data-line="${item.id}"]`);
    if (there) { if (!many) there.querySelector("input").focus(); return; }
    const empty = lines.querySelector(".empty-note");
    if (empty) empty.remove();
    lines.append(lineNode({ item_id: item.id, name: item.name, unit: item.unit, quantity: item.quantity, qty }, true));
    if (!many) lines.lastElementChild.querySelector("input").select();
    submitter.say("");
  };
  const addMany = (pairs) => {
    const room = Math.max(MAX_LINES - lines.querySelectorAll("[data-line]").length, 0);
    pairs.slice(0, room).forEach(([item, qty]) => addLine(item, qty, true));
    if (room) toast(t("ord_added_n", { n: localDigits(String(Math.min(pairs.length, room))) }));
    if (pairs.length > room) alertMessage(t("ord_max_lines", { n: localDigits(String(MAX_LINES)) }));
  };
  const onOrder = () => new Set([...lines.querySelectorAll("[data-line]")].map((li) => Number(li.dataset.line)));
  picker(addBox.querySelector("[data-item-q]"), addBox.querySelector("[data-item-list]"),
         () => ownFirst(current && current.supplier_id), (item) => addLine(item), null,
         { top: () => (current ? supplierLines(current.supplier_id, onOrder(), addMany) : []) });

  // Enter in a quantity never sends the order: it moves on to the next box, then to Save.
  lines.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.classList.contains("qty-input")) return;
    event.preventDefault();
    const boxes = [...lines.querySelectorAll(".qty-input")];
    const next = boxes[boxes.indexOf(event.target) + 1] || form.querySelector("[data-save-order]:not([hidden])");
    if (next) next.focus();
  });

  lines.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-line]");
    if (remove && !orderDialog.dataset.busy && !submitter.unknown) remove.closest("li").remove();
    const notHere = event.target.closest("[data-not-here]");
    if (notHere) markMissing(notHere);
  });

  async function markMissing(node) {
    if (node.disabled || orderDialog.dataset.busy || !current || current.status !== "sent") return;
    if (!twoTaps(node)) return;
    const orderId = current.id;
    lines.querySelectorAll("[data-not-here]").forEach((b) => { b.disabled = true; });
    const result = await save("POST", `/api/v1/inventory/orders/${orderId}/missing`, { item_id: Number(node.dataset.notHere) },
                              { key: newKey(), label: t("ord_not_here_of", { name: node.dataset.name }) });
    lines.querySelectorAll("[data-not-here]").forEach((b) => { b.disabled = false; });
    window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
    if (!result.settled) {
      submitter.say(t("ord_not_sure"));
      const again = await get(`/api/v1/inventory/orders/${orderId}`);
      if (isOk(again) && orderDialog.open && current && current.id === orderId) render(again.data.order);
      loadOrders();
      return;
    }
    if (!isOk(result)) { submitter.say(errorMessage(result, t("failed"))); return; }
    if (orderDialog.open && current && current.id === orderId) {
      render(result.data.order);
      // The button is gone: keep the keyboard in the list (the next "doesn't have it", else the main button).
      const next = lines.querySelector("[data-not-here]") || orderDialog.querySelector("[data-submit]:not([hidden])");
      if (next) next.focus();
    }
    submitter.say("");
    toast(movedText(result.data.moved, node.dataset.name) || t("ord_missing_done", { name: node.dataset.name }));
    loadOrders();
  }

  let loading = 0;
  openOrder = async (id, opener) => {
    if (orderDialog.open) return;  // a second tap must not wipe what is being typed
    if (submitter.holdsUnknown()) { openDialog(orderDialog, opener); return; }
    const ticket = ++loading;
    const result = await get(`/api/v1/inventory/orders/${id}`);
    if (ticket !== loading || orderDialog.open) return;  // only the last tap opens
    if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
    submitter.reset();
    waBox.hidden = true;
    render(result.data.order);
    openDialog(orderDialog, opener);
  };

  // A draft's lines as left, checked here first.
  function draftBody() {
    const now = draftNow();
    for (const line of now.lines) if (!line.qty || line.qty > 100000) { submitter.say(t("bad_qty")); return null; }
    return now;
  }

  async function saveDraft() {
    const body = draftBody();
    if (!body) return null;
    const result = await submitter.run("PUT", `/api/v1/inventory/orders/${current.id}`, body, t("ord_saved_label", { name: current.supplier_name }));
    if (!result || !isOk(result)) return null;
    render(result.data.order);
    return result;
  }

  // WhatsApp: a blank window opens inside the tap (before any wait), then gets the link. If the browser blocked
  // it, the big "Open WhatsApp" button stays (design H1 flow 3).
  async function sendNow() {
    const win = window.open("", "_blank");
    if (win) { try { win.opener = null; } catch (e) { /* ignore */ } }
    if (current.status === "draft") {
      if (!lines.querySelector("[data-line]")) { if (win) win.close(); submitter.say(t("ord_add_first")); return; }
      if (changed() && !(await saveDraft())) { if (win) win.close(); return; }
    }
    const result = await submitter.run("POST", `/api/v1/inventory/orders/${current.id}/send`, {}, t("ord_sent_label", { name: current.supplier_name }));
    if (!result || !isOk(result)) { if (win) win.close(); return; }
    afterSend(result.data, win);
  }

  function afterSend(answer, win) {
    render(answer.order);
    const link = /^https:\/\/wa\.me\//.test(answer.link || "") ? answer.link : "";
    waLink.href = link;
    waBox.hidden = !link;
    if (win && !win.closed) { if (link) win.location.href = link; else win.close(); }
    toast(t("ord_sent_toast", { name: answer.order.supplier_name }));
    loadOrders();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (orderDialog.dataset.busy || !current) return;
    if (submitter.unknown) {  // "Try again": the same request with the same key
      const result = await submitter.run();
      if (!result || !isOk(result)) return;
      if (result.data.link) afterSend(result.data, null);
      else if (result.data.order) { render(result.data.order); loadOrders(); }
      return;
    }
    if (current.status === "draft") { await sendNow(); return; }
    if (current.status === "sent") {
      const order = current;
      orderDialog.close();
      openReceive(order, ordersList);
    }
  });

  saveButton.addEventListener("click", async () => {
    if (orderDialog.dataset.busy || submitter.unknown) return;
    if (!changed()) { toast(t("saved")); return; }
    if (await saveDraft()) { toast(t("ord_saved_label", { name: current.supplier_name })); loadOrders(); }
  });


  cancelButton.addEventListener("click", async () => {
    if (orderDialog.dataset.busy || submitter.unknown || !twoTaps(cancelButton)) return;
    const result = await submitter.run("POST", `/api/v1/inventory/orders/${current.id}/cancel`, {}, t("ord_cancelled_label", { name: current.supplier_name }));
    if (!result || !isOk(result)) return;
    render(result.data.order);
    toast(t("ord_cancelled_label", { name: current.supplier_name }));
    loadOrders();
  });
}

// --- receive what came (design H1 flow 5) ---

const receiveDialog = document.getElementById("receive-dialog");
let openReceive = () => {};
if (receiveDialog) {
  const form = receiveDialog.querySelector("form");
  const submitter = new Submitter(receiveDialog);
  const list = receiveDialog.querySelector("[data-receive-lines]");
  const total = receiveDialog.querySelector("[data-receive-total]");
  wireClose(receiveDialog);
  let order = null;

  const rows = () => [...list.querySelectorAll("[data-line]")];
  const sum = () => {
    let piastres = 0;
    for (const li of rows()) {
      const qty = parseWhole(li.querySelector('[name="qty"]').value) || 0;
      const priceField = li.querySelector('[name="unit_price"]');
      const price = priceField ? parsePiastres(priceField.value || "0") || 0 : 0;
      piastres += qty * price;
      const over = li.querySelector("[data-over]");
      const left = Number(li.dataset.left);
      over.hidden = qty <= left;
      over.textContent = t("ord_more_than_left", { n: localDigits(left) });
    }
    if (total) total.textContent = t("ord_total", { amount: moneyText(piastres) });
    return piastres;
  };
  receiveDialog.addEventListener("input", sum);

  openReceive = (o, opener) => {
    if (submitter.holdsUnknown()) { openDialog(receiveDialog, opener); return; }
    order = o;
    form.reset();
    submitter.reset();
    receiveDialog.querySelector("[data-name]").textContent = o.supplier_name;
    list.replaceChildren(...o.lines.map((line) => {
      const left = Math.max(line.qty - line.received, 0);
      const li = el("li");
      li.dataset.line = String(line.item_id);
      li.dataset.left = String(left);
      const main = el("div", "mat-main");
      main.append(el("strong")); main.firstChild.append(bdi(line.name));
      const said = [t("ord_asked", { n: localDigits(line.qty), unit: line.unit || "" })];
      if (line.received) said.push(t("ord_got_before", { n: localDigits(line.received) }));
      main.append(el("p", "muted", said.join(" · ")));
      const over = el("p", "warn-text", "");
      over.dataset.over = "";
      over.hidden = true;
      main.append(over);
      const side = el("div", "row-actions wrap");
      const qty = el("label", "field small-field", t("ord_came"));
      const qtyInput = el("input");
      Object.assign(qtyInput, { name: "qty", dir: "ltr", inputMode: "numeric", autocomplete: "off", maxLength: 7, value: String(left) });
      qty.append(qtyInput);
      side.append(qty);
      if (perms.money) {
        const price = el("label", "field small-field", t("ord_unit_price"));
        const priceInput = el("input");
        Object.assign(priceInput, { name: "unit_price", dir: "ltr", inputMode: "decimal", autocomplete: "off", maxLength: 12,
                                    value: line.unit_cost ? amountInput(line.unit_cost) : "" });
        price.append(priceInput);
        side.append(price);
      }
      li.append(main, side);
      return li;
    }));
    sum();
    openDialog(receiveDialog, opener);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (receiveDialog.dataset.busy) return;
    let body = null;
    if (!submitter.unknown) {
      const sent = [];
      for (const li of rows()) {
        const qty = parseWhole(li.querySelector('[name="qty"]').value || "0");
        if (qty === null || qty > 100000) { submitter.say(t("bad_qty")); li.querySelector('[name="qty"]').focus(); return; }
        const line = { item_id: Number(li.dataset.line), qty };
        const priceField = li.querySelector('[name="unit_price"]');
        if (priceField && qty > 0) {
          const price = parsePiastres(priceField.value);
          if (price === null || !priceField.value.trim()) { submitter.say(t("ord_need_price")); priceField.focus(); return; }
          line.unit_price = price;
        }
        sent.push(line);
      }
      const close = form.elements.namedItem("close").checked;
      if (!sent.some((l) => l.qty > 0) && !close) { submitter.say(t("ord_nothing_came")); return; }
      body = { lines: sent, close };
      if (perms.money && sum() > 0) {
        const paid = form.querySelector('[name="paid"]:checked');
        if (!paid) { submitter.say(t("ord_need_paid")); return; }
        body.paid = paid.value;
      }
    }
    const result = await submitter.run("POST", `/api/v1/inventory/orders/${order.id}/receive`, body, t("ord_received_label", { name: order.supplier_name }));
    if (!result || !isOk(result)) return;
    // The pick lists' "you have" follows what came (until the page reloads).
    for (const line of (body && body.lines) || []) {
      const item = items.find((i) => i.id === line.item_id);
      if (item) item.quantity += line.qty;
    }
    receiveDialog.close();
    const moved = movedText(result.data.moved);
    toast([t(result.data.order.status === "done" ? "ord_received_done" : "ord_received_part", { name: order.supplier_name }), moved]
      .filter(Boolean).join(" · "));
    // Came after it was moved to a backup supplier: that order may now bring it twice.
    const late = (result.data.late || []).filter((l) => l.moved_to === "supplier");
    if (late.length) alertMessage(late.map((l) => t("ord_late", { item: itemName(l.item_id), name: l.supplier_name })).join(" "));
    window.dispatchEvent(new CustomEvent("hamada:stock-changed"));
    loadOrders();
  });
}

// --- a new order by hand ---

const newOrderDialog = document.getElementById("new-order-dialog");
let startOrder = () => {};
if (newOrderDialog) {
  const form = newOrderDialog.querySelector("form");
  const submitter = new Submitter(newOrderDialog);
  const select = form.elements.namedItem("supplier_id");
  const q = newOrderDialog.querySelector("[data-item-q]");
  let picked = null;
  let pickedMany = null;  // [[item, qty], ...] from "all that is short" or "all its items"
  wireClose(newOrderDialog);
  const qtyField = form.elements.namedItem("qty");
  const pickedLine = newOrderDialog.querySelector("[data-picked]");
  const show = () => {
    q.placeholder = t("ord_type_item");
    pickedLine.textContent = pickedMany ? t("ord_picked_n", { n: localDigits(String(pickedMany.length)) })
      : picked ? t("ord_picked", { name: picked.name }) : "";
    qtyField.closest(".field").hidden = Boolean(pickedMany);
  };
  picker(q, newOrderDialog.querySelector("[data-item-list]"), () => ownFirst(Number(select.value)),
         (item) => { picked = item; pickedMany = null; show(); qtyField.focus(); }, null,
         { top: () => supplierLines(Number(select.value), new Set(), (pairs) => {
           if (pairs.length > MAX_LINES) alertMessage(t("ord_max_lines", { n: localDigits(String(MAX_LINES)) }));
           pickedMany = pairs.slice(0, MAX_LINES); picked = null; show();
         }) });
  select.addEventListener("change", () => { if (pickedMany) { pickedMany = null; show(); } });

  startOrder = (supplierId, opener) => {
    if (submitter.holdsUnknown()) { openDialog(newOrderDialog, opener); return; }
    if (!suppliers.some((s) => s.is_active)) { alertMessage(t("ord_need_supplier")); return; }
    form.reset();
    submitter.reset();
    picked = null;
    pickedMany = null;
    show();
    if (supplierId) select.value = String(supplierId);
    openDialog(newOrderDialog, opener);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (newOrderDialog.dataset.busy) return;
    let body = null;
    if (!submitter.unknown) {
      if (!select.value) { submitter.say(t("ord_pick_supplier")); select.focus(); return; }
      if (pickedMany) {
        body = { supplier_id: Number(select.value), lines: pickedMany.map(([item, qty]) => ({ item_id: item.id, qty: Math.min(qty, 100000) })) };
      } else {
        if (!picked) { submitter.say(t("ord_pick_item")); q.focus(); return; }
        const qty = parseWhole(qtyField.value);
        if (!qty || qty > 100000) { submitter.say(t("bad_qty")); return; }
        body = { supplier_id: Number(select.value), lines: [{ item_id: picked.id, qty }] };
      }
    }
    const result = await submitter.run("POST", "/api/v1/inventory/orders", body, t("ord_started_label"));
    if (!result || !isOk(result)) return;
    newOrderDialog.close();
    loadOrders();
    openOrder(result.data.order.id, ordersList);
  });
}

// --- short items that nobody is bringing (design H1 backup): pick a supplier for each ---

const needsBox = document.querySelector("[data-needs]");
const needsList = document.querySelector("[data-needs-list]");
const needsCount = document.querySelector("[data-needs-count]");
let needsTicket = 0;  // goes up when a row is ordered or dismissed here

function needText(need) {
  const n = localDigits(need.qty);
  return need.kind === "short" ? t("ord_need_short", { n, unit: need.unit || "" }) : t("ord_need_none", { n, unit: need.unit || "" });
}

function needRow(need) {
  const li = el("li");
  li.dataset.need = String(need.item_id);
  li.dataset.name = need.name;
  const main = el("div", "mat-main");
  main.append(el("strong")); main.firstChild.append(bdi(need.name));
  main.append(el("p", "muted"));
  const qty = el("input", "qty-input");
  Object.assign(qty, { dir: "ltr", inputMode: "numeric", autocomplete: "off", maxLength: 7 });
  qty.setAttribute("aria-label", t("ord_qty_of", { name: need.name }));
  const combo = el("div", "combo");
  const sup = el("input");
  Object.assign(sup, { dir: "auto", autocomplete: "off", maxLength: 120, placeholder: t("ord_supplier_ph") });
  const listId = `need-sup-${need.item_id}`;
  for (const [k, v] of Object.entries({ role: "combobox", "aria-expanded": "false", "aria-controls": listId, "aria-autocomplete": "list",
                                        "aria-label": t("ord_supplier_for", { name: need.name }), "data-need-supplier": "" })) sup.setAttribute(k, v);
  const drop = el("ul", "combo-list");
  Object.assign(drop, { id: listId, hidden: true });
  drop.setAttribute("role", "listbox");
  combo.append(sup, drop);
  const side = el("div", "row-actions wrap");
  side.append(qty, el("span", "muted", need.unit || ""), combo, button(t("ord_need_order"), { "data-need-order": "" }, "btn"),
              button(t("ord_need_dismiss"), { "data-need-dismiss": "", "aria-label": t("ord_need_dismiss_of", { name: need.name }) }));
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  li.append(main, side, error);
  picker(sup, drop, () => suppliers.filter((x) => x.is_active), (x) => { li.dataset.supplier = String(x.id); error.textContent = ""; return x.name; },
         perms.manage ? (name) => {
           // The supplier dialog, its name filled in; once saved, the new supplier is this row's (Saged, 2026-11-26).
           editSupplier(null, sup, { name, onSaved: (x) => { li.dataset.supplier = String(x.id); sup.value = x.name; error.textContent = ""; } });
           return name;
         } : null);
  sup.addEventListener("input", () => { delete li.dataset.supplier; });
  return li;
}

// After a row goes, the keyboard moves to the next row (or the box's title), never to the page's start.
function focusAfter(li) {
  const next = li.nextElementSibling || li.previousElementSibling;
  const target = next && next !== li ? next.querySelector(".qty-input") : document.getElementById("needs-title");
  if (target) target.focus();
}

function renderNeeds(list) {
  if (!needsBox) return;
  // Rows stay in place when the list comes again, so what is being typed in one is kept.
  const want = new Set(list.map((need) => String(need.item_id)));
  for (const li of [...needsList.children]) if (!want.has(li.dataset.need)) li.remove();
  for (const need of list) {
    let li = needsList.querySelector(`[data-need="${need.item_id}"]`);
    if (!li) { li = needRow(need); needsList.append(li); }
    li.querySelector(".mat-main p").textContent = needText(need);
    const qty = li.querySelector(".qty-input");
    if (!qty.value || qty.value === li.dataset.qtyShown) { qty.value = String(need.qty); li.dataset.qtyShown = qty.value; }
    li.querySelector("[data-need-dismiss]").hidden = need.kind !== "short";
  }
  needsBox.hidden = !list.length;
  if (needsCount) needsCount.textContent = list.length ? `(${localDigits(list.length)})` : "";
}

async function orderNeed(li) {
  const error = li.querySelector(".form-error");
  const go = li.querySelector("[data-need-order]");
  const sup = li.querySelector("[data-need-supplier]");
  if (go.disabled) return;
  const qty = parseWhole(li.querySelector(".qty-input").value);
  if (!qty || qty < 1) { error.textContent = t("ord_need_qty"); li.querySelector(".qty-input").focus(); return; }
  let id = Number(li.dataset.supplier || 0);
  if (!id) {
    const typed = plain(sup.value.trim());
    const match = typed && suppliers.find((x) => x.is_active && plain(x.name) === typed);
    if (match) id = match.id;
  }
  if (!id) { error.textContent = t("ord_pick_supplier"); sup.focus(); return; }
  go.disabled = true;
  error.textContent = "";
  const result = await save("POST", `/api/v1/inventory/needs/${li.dataset.need}/order`, { supplier_id: id, qty },
                            { key: newKey(), label: t("ord_need_order_of", { name: li.dataset.name }) });
  go.disabled = false;
  window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
  if (!result.settled) { error.textContent = t("ord_not_sure"); loadOrders(); return; }
  if (!isOk(result)) { error.textContent = errorMessage(result, t("failed")); return; }
  needsTicket++;  // a list asked for before this must not bring the row back
  toast(t("ord_need_done", { item: li.dataset.name, name: result.data.order.supplier_name }));
  focusAfter(li);
  li.remove();
  if (!needsList.children.length) needsBox.hidden = true;
  loadOrders();
}

async function dismissNeed(li, node) {
  const error = li.querySelector(".form-error");
  if (node.disabled || !twoTaps(node)) return;
  node.disabled = true;
  const result = await save("POST", `/api/v1/inventory/needs/${li.dataset.need}/dismiss`, {},
                            { key: newKey(), label: t("ord_need_dismiss_of", { name: li.dataset.name }) });
  node.disabled = false;
  if (!result.settled) { error.textContent = t("ord_not_sure"); loadOrders(); return; }
  if (!isOk(result)) { error.textContent = errorMessage(result, t("failed")); return; }
  needsTicket++;
  focusAfter(li);
  renderNeeds(result.data.items);
  loadOrders();
}

if (needsList) {
  needsList.addEventListener("click", (event) => {
    const li = event.target.closest("[data-need]");
    if (!li) return;
    if (event.target.closest("[data-need-order]")) orderNeed(li);
    const dismiss = event.target.closest("[data-need-dismiss]");
    if (dismiss) dismissNeed(li, dismiss);
  });
  // Enter in the quantity moves on to the supplier.
  needsList.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.classList.contains("qty-input")) return;
    event.preventDefault();
    const li = event.target.closest("[data-need]");
    if (li) li.querySelector("[data-need-supplier]").focus();
  });
}

// --- suppliers ---

function fillSelects() {
  document.querySelectorAll("[data-supplier-select]").forEach((select) => {
    const value = select.value;
    const first = select.options[0];
    const options = suppliers.filter((s) => s.is_active || String(s.id) === value).map((s) => {
      const option = document.createElement("option");
      option.value = s.id;
      option.textContent = s.name;
      return option;
    });
    select.replaceChildren(first, ...options);
    select.value = value;
  });
  const hint = document.querySelector("[data-no-suppliers]");
  if (hint) hint.hidden = suppliers.length > 0;
}

function owedText(owed) {
  if (owed > 0) return t("ord_we_owe", { amount: moneyText(owed) });
  if (owed < 0) return t("ord_they_owe", { amount: moneyText(-owed) });
  return t("ord_owe_nothing");
}

// "Comes on Saturday and Tuesday at 11:00 am", the week from Saturday as in Egypt.
const WEEK = [5, 6, 0, 1, 2, 3, 4];

function visitNode(s) {
  const days = WEEK.filter((d) => (s.visit_days || []).includes(d)).map((d) => t(`wd_${d}`));
  if (!days.length) return null;
  let list = days.join("، ");
  try { list = new Intl.ListFormat(document.documentElement.lang || "ar", { type: "conjunction" }).format(days); }
  catch (e) { /* an old browser: commas */ }
  if (!s.visit_time) return document.createTextNode(t("ord_comes", { days: list }));
  const span = el("span");
  const [before, after] = t("ord_comes_at", { days: list }).split("{time}");
  span.append(before, clockNode(`T${s.visit_time}`), after || "");
  return span;
}

function renderSuppliers() {
  if (!suppliersList) return;
  suppliersList.replaceChildren();
  if (!suppliers.length) {
    suppliersList.append(el("li", "empty-note", t(perms.manage ? "ord_no_suppliers_add" : "ord_no_suppliers")));
    return;
  }
  for (const s of suppliers) {
    const li = el("li");
    li.dataset.supplier = String(s.id);
    const main = el("div", "mat-main");
    main.append(el("strong")); main.firstChild.append(bdi(s.name));
    if (!s.is_active) main.append(" ", el("span", "pill grey", t("ord_stopped")));
    const parts = [];
    const p = el("p", "muted");
    if (s.phone) { const phone = el("bdi", "ltr num", phoneText(s.phone)); p.append(phone); }
    else p.append(t("ord_no_phone"));
    if (s.owed != null) parts.push(owedText(s.owed));
    if (parts.length) p.append(` · ${parts.join(" · ")}`);
    main.append(p);
    const visit = visitNode(s);
    const count = items.filter((i) => i.supplier_id === s.id).length;
    if (visit || count) {
      const q = el("p", "muted");
      if (visit) q.append(visit);
      if (visit && count) q.append(" · ");
      if (count) q.append(t("ord_items_count", { n: localDigits(String(count)) }));
      main.append(q);
    }
    const side = el("div", "row-actions wrap");
    if (s.is_active) side.append(button(t("ord_order_from"), { "data-order-from": String(s.id) }));
    if (perms.money) side.append(button(t("ord_account"), { "data-account": String(s.id) }));
    if (perms.manage) side.append(button(t("edit"), { "data-edit-supplier": String(s.id) }));
    li.append(main, side);
    suppliersList.append(li);
  }
}

async function loadSuppliers() {
  const result = await get("/api/v1/inventory/suppliers?all=1");
  if (!isOk(result)) { if (suppliersStatus) suppliersStatus.textContent = errorMessage(result, t("failed")); return; }
  if (suppliersStatus) suppliersStatus.textContent = "";
  suppliers = result.data.suppliers;
  renderSuppliers();
  fillSelects();
}

// Add or edit a supplier; editing sends only what changed.
const supplierDialog = document.getElementById("supplier-dialog");
let editSupplier = () => {};
if (supplierDialog) {
  const form = supplierDialog.querySelector("form");
  const submitter = new Submitter(supplierDialog);
  const field = (name) => form.elements.namedItem(name);
  let editing = null;
  const linkList = supplierDialog.querySelector("[data-link-items]");
  const linkFilter = supplierDialog.querySelector("[data-link-filter]");
  const linkCount = supplierDialog.querySelector("[data-link-count]");
  const linkAll = supplierDialog.querySelector("[data-link-all]");
  let linked = new Set();   // the item ids ticked now
  let linkedBefore = new Set();
  wireClose(supplierDialog);

  const countText = () => (linked.size ? t("ord_link_count", { n: localDigits(String(linked.size)) }) : "");

  // The tick boxes: this supplier's items first, then the rest by name; typing filters them.
  function renderLinks() {
    const own = editing ? editing.id : null;
    const word = linkFilter.value.trim().toLowerCase();
    const shown = items.filter((i) => !word || i.name.toLowerCase().includes(word))
      .sort((a, b) => (linkedBefore.has(b.id) - linkedBefore.has(a.id)) || a.name.localeCompare(b.name, document.documentElement.lang || "ar"));
    linkList.replaceChildren();
    for (const i of shown) {
      const li = el("li");
      const label = el("label", "check");
      const box = el("input");
      box.type = "checkbox";
      box.value = String(i.id);
      box.checked = linked.has(i.id);
      label.append(box, bdi(i.name));
      if (i.supplier_id && i.supplier_id !== own) {
        const other = suppliers.find((x) => x.id === i.supplier_id);
        if (other) {
          const note = el("span", "muted");
          const [before, after] = t("ord_link_other").split("{name}");
          note.append(before, bdi(other.name), after || "");
          label.append(" ", note);
        }
      }
      li.append(label);
      linkList.append(li);
    }
    if (!shown.length && items.length) linkList.append(el("li", "muted", t("ord_link_none")));
    supplierDialog.querySelector("[data-link-empty]").hidden = items.length > 0;
    linkFilter.hidden = items.length < 8;
    linkCount.textContent = countText();
    markAll();
  }
  // "Select all" ticks only what the search shows (Saged, 2026-11-26).
  const boxesShown = () => [...linkList.querySelectorAll("input[type=checkbox]")];
  function markAll() {
    if (!linkAll) return;
    const boxes = boxesShown();
    const on = boxes.filter((b) => b.checked).length;
    linkAll.closest("label").hidden = boxes.length < 2;
    linkAll.checked = boxes.length > 0 && on === boxes.length;
    linkAll.indeterminate = on > 0 && on < boxes.length;
  }
  if (linkAll) linkAll.addEventListener("change", () => {
    for (const box of boxesShown()) {
      box.checked = linkAll.checked;
      if (linkAll.checked) linked.add(Number(box.value)); else linked.delete(Number(box.value));
    }
    linkCount.textContent = countText();
    markAll();
  });
  linkList.addEventListener("change", (event) => {
    const id = Number(event.target.value);
    if (event.target.checked) linked.add(id); else linked.delete(id);
    linkCount.textContent = countText();
    markAll();
  });
  linkFilter.addEventListener("input", renderLinks);
  linkFilter.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });

  const dayBoxes = () => [...form.querySelectorAll("[name=visit_day]")];
  const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

  // Under a field: what is wrong with it.
  const fieldError = (name, text) => {
    const holder = form.querySelector(`[data-field-error="${name}"]`);
    if (holder) holder.textContent = text;
    if (text) field(name).setAttribute("aria-invalid", "true"); else field(name).removeAttribute("aria-invalid");
  };
  // The suppliers already here: the name list, and "already here" when the name is one of them.
  const others = () => suppliers.filter((x) => !editing || x.id !== editing.id);
  const existsNote = supplierDialog.querySelector("[data-name-exists]");
  let same = null;
  const checkName = () => {
    const name = plain(field("name").value.trim().replace(/\s+/g, " "));
    same = name ? others().find((x) => plain(x.name.trim().replace(/\s+/g, " ")) === name) || null : null;
    existsNote.hidden = !same;
  };
  let onSaved = null;  // what the "need a supplier" row does with the new supplier
  const switchTo = (supplier) => { const after = onSaved; fill(supplier); onSaved = after; field("name").focus(); };
  picker(field("name"), supplierDialog.querySelector("[data-name-list]"),
         () => others().map((x) => ({ ...x, tag: t("ord_exists") })), (x) => { switchTo(x); return x.name; }, null,
         { quiet: true, noFocus: true });
  field("name").addEventListener("input", () => { checkName(); fieldError("name", ""); });
  field("phone").addEventListener("input", () => fieldError("phone", ""));
  supplierDialog.querySelector("[data-open-existing]").addEventListener("click", () => { if (same) switchTo(same); });

  editSupplier = (supplier, opener, options = {}) => {
    if (submitter.holdsUnknown()) { onSaved = options.onSaved || null; openDialog(supplierDialog, opener); return; }
    fill(supplier, options);
    openDialog(supplierDialog, opener);
  };
  function fill(supplier, options = {}) {
    editing = supplier;
    onSaved = options.onSaved || null;
    form.reset();
    submitter.reset();
    fieldError("name", "");
    fieldError("phone", "");
    supplierDialog.querySelectorAll("[data-title-new]").forEach((n) => { n.hidden = Boolean(supplier); });
    supplierDialog.querySelectorAll("[data-title-edit]").forEach((n) => { n.hidden = !supplier; });
    if (supplier) {
      field("name").value = supplier.name;
      field("phone").value = supplier.phone || "";
      field("notes").value = supplier.notes || "";
      field("is_active").checked = supplier.is_active;
      field("visit_time").value = supplier.visit_time || "";
      dayBoxes().forEach((box) => { box.checked = (supplier.visit_days || []).includes(Number(box.value)); });
    }
    // The backup: any other supplier; one that stopped is still shown when it is the one chosen.
    // (A page from before the backup field has none: the dialog still opens.)
    const backup = field("backup_id");
    const chosen = supplier && supplier.backup_id ? String(supplier.backup_id) : "";
    if (backup) {
      if (chosen && ![...backup.options].some((o) => o.value === chosen)) {
        const gone = suppliers.find((x) => String(x.id) === chosen);
        if (gone) backup.append(new Option(gone.name, chosen));
      }
      [...backup.options].forEach((o) => { o.hidden = o.disabled = Boolean(supplier) && o.value === String(supplier.id); });
      backup.value = chosen;
    }
    linkedBefore = new Set(supplier ? items.filter((i) => i.supplier_id === supplier.id).map((i) => i.id) : []);
    linked = new Set(linkedBefore);
    linkFilter.value = "";
    if (!supplier && options.name) field("name").value = options.name;
    renderLinks();
    checkName();
    // A supplier from before the rule: say at once what is missing.
    if (supplier) {
      if (!twoWords(supplier.name)) fieldError("name", t("ord_name_two"));
      if (!isMobile(supplier.phone)) fieldError("phone", t("ord_phone_mobile"));
    }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (supplierDialog.dataset.busy) return;
    let method = "POST";
    let url = "/api/v1/inventory/suppliers";
    let body = null;
    if (!submitter.unknown) {
      const v = { name: field("name").value.trim(), phone: field("phone").value.trim(), notes: field("notes").value.trim() };
      const badName = !twoWords(v.name);
      const badPhone = !isMobile(v.phone);
      fieldError("name", badName ? t("ord_name_two") : "");
      fieldError("phone", badPhone ? t("ord_phone_mobile") : "");
      if (badName || badPhone) { field(badName ? "name" : "phone").focus(); return; }
      if (same) { existsNote.hidden = false; supplierDialog.querySelector("[data-open-existing]").focus(); return; }
      const days = dayBoxes().filter((box) => box.checked).map((box) => Number(box.value)).sort((a, b) => a - b);
      const time = field("visit_time").value;
      const backupField = field("backup_id");
      const backupId = backupField && backupField.value ? Number(backupField.value) : (backupField ? null : editing && editing.backup_id) || null;
      if (editing) {
        v.is_active = field("is_active").checked;
        body = {};
        for (const [k, value] of Object.entries(v)) if (value !== (editing[k] ?? "")) body[k] = value;
        if (days.join() !== [...(editing.visit_days || [])].sort((a, b) => a - b).join()) body.visit_days = days;
        if (time !== (editing.visit_time || "")) body.visit_time = time;
        if (backupId !== (editing.backup_id || null)) body.backup_id = backupId;
        if (!sameSet(linked, linkedBefore)) body.item_ids = [...linked];
        if (!Object.keys(body).length) { supplierDialog.close(); return; }
        method = "PATCH";
        url = `/api/v1/inventory/suppliers/${editing.id}`;
      } else {
        body = { ...v, visit_days: days, visit_time: time };
        if (backupId) body.backup_id = backupId;
        if (linked.size) body.item_ids = [...linked];
      }
    }
    const result = await submitter.run(method, url, body, t("ord_supplier_saved", { name: field("name").value.trim() }));
    if (result && result.settled && !isOk(result)) {
      const fields = (result.data && result.data.error && result.data.error.fields) || {};
      if (fields.name === "two_words") fieldError("name", t("ord_name_two"));
      if (fields.phone === "mobile") fieldError("phone", t("ord_phone_mobile"));
    }
    if (!result || !isOk(result)) return;
    const done = onSaved;
    onSaved = null;
    supplierDialog.close();
    if (done) done(result.data.supplier);
    toast(t("ord_supplier_saved", { name: result.data.supplier.name }));
    // The page's own copy of who each item comes from, as the server now has it.
    const id = result.data.supplier.id;
    const now = new Set(result.data.item_ids || []);
    let moved = false;
    for (const i of items) {
      const want = now.has(i.id) ? id : (i.supplier_id === id ? null : i.supplier_id);
      if (want !== i.supplier_id) { i.supplier_id = want; moved = true; }
      if (now.has(i.id) && i.backup_supplier_id === id) i.backup_supplier_id = null;
    }
    loadSuppliers();
    if (moved) window.dispatchEvent(new CustomEvent("hamada:stock-changed"));
  });
}

// --- a supplier's account: bills and payments (money.view) ---

const accountDialog = document.getElementById("account-dialog");
let openAccount = async () => {};
let accountOf = null;
if (accountDialog) {
  const list = accountDialog.querySelector("[data-entries]");
  const status = accountDialog.querySelector("[data-entries-status]");
  wireClose(accountDialog);

  function entryNode(entry) {
    const li = el("li");
    const main = el("div", "mat-main");
    const head = el("strong", "", t(entry.kind === "bill" ? "ord_bill" : "ord_payment"));
    main.append(head, " ", el("span", "muted", dayText(entry.day)));
    if (entry.voided_at) main.append(" ", el("span", "pill grey", t("ord_voided")));
    if (entry.note) { const p = el("p", "muted"); p.append(bdi(entry.note)); main.append(p); }
    const side = el("div", "row-actions");
    side.append(el("span", entry.voided_at ? "muted amt" : "amt", moneyText(entry.amount)));
    if (perms.owner && !entry.voided_at) side.append(button(t("ord_void"), { "data-void": String(entry.id) }, "btn ghost danger"));
    li.append(main, side);
    return li;
  }

  // Closing it goes back to the supplier's "Account" button, even after the list was drawn again.
  const backTo = (supplier) => ({
    isConnected: true,
    focus: () => { const node = document.querySelector(`[data-account="${supplier.id}"]`); if (node) node.focus(); },
  });

  openAccount = async (supplier) => {
    accountOf = supplier;
    accountDialog.querySelector("[data-name]").textContent = supplier.name;
    status.textContent = t("loading");
    list.replaceChildren();
    accountDialog.querySelector("[data-owed]").textContent = "";
    openDialog(accountDialog, backTo(supplier));
    const result = await get(`/api/v1/inventory/suppliers/${supplier.id}/account`);
    if (accountOf !== supplier) return;
    if (!isOk(result)) { status.textContent = errorMessage(result, t("failed")); return; }
    const entries = result.data.entries;
    const owed = entries.filter((e) => !e.voided_at).reduce((n, e) => n + (e.kind === "bill" ? e.amount : -e.amount), 0);
    accountDialog.querySelector("[data-owed]").textContent = owedText(owed);
    status.textContent = entries.length ? "" : t("ord_no_entries");
    list.replaceChildren(...entries.map(entryNode));
  };

  list.addEventListener("click", async (event) => {
    const node = event.target.closest("[data-void]");
    if (!node || node.disabled || !twoTaps(node)) return;
    node.disabled = true;
    const result = await save("POST", `/api/v1/inventory/vendor-entries/${node.dataset.void}/void`, {}, { key: newKey(), label: t("ord_void") });
    node.disabled = false;
    window.dispatchEvent(new CustomEvent("hamada:pending-changed"));
    if (!result.settled) { alertMessage(t("ord_not_sure")); }
    else if (!isOk(result)) { alertMessage(errorMessage(result, t("failed"))); return; }
    else toast(t("ord_voided_toast"));
    openAccount(accountOf);
    loadSuppliers();
  });
}

// A bill or a payment.
const entryDialog = document.getElementById("entry-dialog");
let addEntry = () => {};
if (entryDialog) {
  const form = entryDialog.querySelector("form");
  const submitter = new Submitter(entryDialog);
  const field = (name) => form.elements.namedItem(name);
  let kind = "bill";
  let supplier = null;
  wireClose(entryDialog, () => { if (supplier) openAccount(supplier); });
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  addEntry = (which, who, opener) => {
    if (submitter.holdsUnknown()) { accountDialog.close(); openDialog(entryDialog, opener); return; }
    kind = which;
    supplier = who;
    form.reset();
    submitter.reset();
    entryDialog.querySelector("[data-title-bill]").hidden = kind !== "bill";
    entryDialog.querySelector("[data-title-payment]").hidden = kind !== "payment";
    entryDialog.querySelectorAll("[data-payment-only]").forEach((n) => { n.hidden = kind !== "payment"; });
    entryDialog.querySelector("[data-name]").textContent = who.name;
    field("day").value = today();
    accountDialog.close();
    openDialog(entryDialog, opener);
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (entryDialog.dataset.busy) return;
    let body = null;
    if (!submitter.unknown) {
      const amount = parsePiastres(field("amount").value);
      if (!amount || amount <= 0) { submitter.say(t("bad_amount")); return; }
      if (!field("day").value) { submitter.say(t("ord_need_day")); return; }
      body = { amount, day: field("day").value, note: field("note").value.trim() };
      if (kind === "payment") body.from_shift = Boolean(field("from_shift") && field("from_shift").checked);
    }
    const url = `/api/v1/inventory/suppliers/${supplier.id}/${kind === "bill" ? "bills" : "payments"}`;
    const result = await submitter.run("POST", url, body, t(kind === "bill" ? "ord_bill_saved" : "ord_payment_saved", { name: supplier.name }));
    if (!result || !isOk(result)) return;
    entryDialog.close();
    toast(t(kind === "bill" ? "ord_bill_saved" : "ord_payment_saved", { name: supplier.name }));
    loadSuppliers();
    openAccount(supplier);
  });
}

// --- one listener for the buttons in the lists ---

const supplierOf = (id) => suppliers.find((s) => s.id === Number(id));
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-open-order], [data-new-order], [data-order-from], [data-account], [data-edit-supplier], [data-new-supplier], [data-entry]");
  if (!target) return;
  if (target.dataset.openOrder) openOrder(Number(target.dataset.openOrder), target);
  else if (target.hasAttribute("data-new-order")) startOrder(null, target);
  else if (target.dataset.orderFrom) startOrder(Number(target.dataset.orderFrom), target);
  else if (target.dataset.account) { const s = supplierOf(target.dataset.account); if (s) openAccount(s); }
  else if (target.dataset.editSupplier) { const s = supplierOf(target.dataset.editSupplier); if (s) editSupplier(s, target); }
  else if (target.hasAttribute("data-new-supplier")) editSupplier(null, target);
  else if (target.dataset.entry && accountOf) addEntry(target.dataset.entry, accountOf, target);
});

renderOrders(data.orders || []);
renderNeeds(data.needs || []);
renderSuppliers();
fillSelects();

// A save retried from the pending list (shell.js) may have changed orders, suppliers or stock: show them again.
window.addEventListener("hamada:pending-settled", () => {
  loadOrders().catch(() => {});
  loadSuppliers().catch(() => {});
  window.dispatchEvent(new CustomEvent("hamada:stock-changed"));
});
