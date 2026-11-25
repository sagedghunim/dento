// Today (design 5.11, 7.4): the queue fragment polled with its ETag, one poller per browser and clinic,
// rows patched in place and never moved under the finger, a stale indicator, and one-tap row actions.
import { errorMessage, fetchText, isOk, membership, pendingList, save } from "./api.js";
import { initBook } from "./book.js";
import { later, openPlan } from "./fu.js";
import { parseFragment, receiptUrl } from "./dialogs.js";
import { clockParts, localDigits } from "./numbers.js";
import { initPay } from "./pay.js";
import { alertMessage, renderPending, toast } from "./shell.js";
import { t } from "./strings.js";
import { initVisitDialogs } from "./visit.js";
import { initWalkIn } from "./walkin.js";
import { showReopen } from "./reopen.js";

const card = document.querySelector("[data-queue-card]");
const list = document.querySelector('[data-slot="groups"]');
const strip = document.querySelector('[data-slot="followups"]');  // "just out of the visit" (web/_just_out.html)
const config = JSON.parse(document.getElementById("clinic-config").textContent);
const meta = document.querySelector('meta[name="hamada"]');
const freshness = document.querySelector("[data-freshness]");
const freshText = document.querySelector("[data-fresh-text]");
const tryNow = document.querySelector("[data-try-now]");

const dayParam = new URLSearchParams(window.location.search).get("day") || "";
const url = "/today/queue" + (dayParam ? `?day=${encodeURIComponent(dayParam)}` : "");
const channelName = `hamada-today-${membership()}-${dayParam || "current"}`;

let etag = card.dataset.etag || null;
let pollAfter = Number(card.dataset.pollAfter) || 5;
let serverTime = card.dataset.serverTime || "";
let clockSkew = 0;  // the server's clock minus this one's, in ms: "didn't come" waits for the booking's time
let lastGood = Date.now();
let failures = 0;
let retryAfter = 0;
let timer = null;
let expectedAt = 0;
let wokeUp = false;
let inFlight = false;
let leader = false;
let stopped = false;
let lastPeerVisible = 0;
let lastPollAt = 0;
let reloadWanted = false;
const rowHtml = new Map();       // id -> the server's HTML of the row now shown
const heldRows = new Map();      // id -> newer server HTML waiting until the row is let go
const acting = new Map();        // "id:action" -> true while a row action is in flight
// Rows the user just changed themself (Instinct 3, Saged 2026-11-25): the server's answer shows at once even with the
// mouse or focus still on the row; holding is only for changes made by someone else.
const mine = new Set();

// --- the channel between tabs: one tab polls, the others listen (design 7.4) ---

let channel = null;
try { channel = new BroadcastChannel(channelName); } catch (e) { channel = null; }
const post = (message) => { if (channel) try { channel.postMessage(message); } catch (e) { /* closed */ } };

if (channel) {
  channel.addEventListener("message", (event) => {
    const m = event.data || {};
    if (m.type === "fragment" && m.etag !== etag) { etag = m.etag; good(m.serverTime); apply(m.html); }
    if (m.type === "fresh" || (m.type === "fragment" && m.etag === etag)) good(m.serverTime);
    if (m.type === "poll-now" && leader) refreshNow();
    if (m.type === "visible") {
      // Another tab is on screen while this one polls: keep the visible pace, without polling twice.
      lastPeerVisible = Date.now();
      if (leader && Date.now() - lastPollAt > pollAfter * 1000) refreshNow();
      else if (leader && expectedAt - Date.now() > pollAfter * 1200) schedule();
    }
  });
}

function becomeLeader() {
  leader = true;
  schedule(0);
}

if (navigator.locks && channel) {
  // Held for the life of the tab; the next tab in line takes over when this one closes.
  navigator.locks.request(`hamada-today-${membership()}-${dayParam || "current"}`, () => new Promise(() => becomeLeader()));
} else {
  becomeLeader();  // plain http on a LAN has no locks: each tab polls on its own
}

// --- polling ---

const visibleNow = () => document.visibilityState === "visible";
function intervalMs() {
  const someoneLooks = visibleNow() || Date.now() - lastPeerVisible < 3 * pollAfter * 1000;
  return (someoneLooks ? pollAfter : Math.max(60, pollAfter)) * 1000;
}

function schedule(ms) {
  clearTimeout(timer);
  if (stopped || !leader) return;
  let delay = ms;
  if (delay == null) {
    if (failures) delay = retryAfter ? retryAfter * 1000 : Math.random() * Math.min(60000, 2000 * 2 ** failures);
    else delay = intervalMs() * (0.8 + Math.random() * 0.4);
  }
  expectedAt = Date.now() + delay;
  timer = setTimeout(() => {
    if (Date.now() - expectedAt > 15000) wokeUp = true;  // the laptop slept: the first failure doesn't count
    poll();
  }, delay);
}

function refreshNow() {
  if (leader) { retryAfter = 0; schedule(0); } else post({ type: "poll-now" });
}

async function poll() {
  if (inFlight || stopped) return;
  inFlight = true;
  lastPollAt = Date.now();
  const result = await fetchText(url, { etag });
  inFlight = false;
  const headers = result.headers;
  retryAfter = Number(headers.get("Retry-After")) || 0;
  if (result.status === 200 || result.status === 304) {
    failures = 0;
    wokeUp = false;
    pollAfter = Number(headers.get("Poll-After")) || pollAfter;
    checkVersion(headers.get("X-App-Version"));
    good(headers.get("X-Server-Time"));
    if (result.status === 200) {
      etag = headers.get("ETag");
      apply(result.text);
      post({ type: "fragment", html: result.text, etag, serverTime });
    } else {
      post({ type: "fresh", etag, serverTime });
    }
  } else if (result.status === 401) {
    stopped = true;  // signed out: not "connection lost"
    window.dispatchEvent(new CustomEvent("hamada:signed-out"));
    return;
  } else if (result.status === 409 && result.data.error && result.data.error.code === "clinic_changed") {
    window.location.reload();
    return;
  } else if (result.status === 403) {
    stopped = true;
    alertMessage(errorMessage(result, t("failed")));
    return;
  } else if (wokeUp) {
    wokeUp = false;
  } else {
    failures += 1;
  }
  showFreshness();
  schedule();
}

function good(time) {
  lastGood = Date.now();
  if (time) serverTime = time;
  const parsed = time ? Date.parse(time) : NaN;
  if (!Number.isNaN(parsed)) clockSkew = parsed - Date.now();
  showFreshness();
}

function showFreshness() {
  const stale = Date.now() - lastGood > 2 * intervalMs() + 2000;
  card.classList.toggle("stale", stale);
  freshness.classList.toggle("stale", stale);
  tryNow.hidden = !stale;
  const parts = clockParts(serverTime);
  const time = parts ? `${parts.time} ${parts.pm ? t("pm") : t("am")}` : "";
  freshText.textContent = stale ? t("stale", { time }) : t("updated", { time });
}
setInterval(showFreshness, 5000);
tryNow.addEventListener("click", () => { failures = 0; retryAfter = 0; refreshNow(); });

for (const name of ["online", "focus", "pageshow"]) window.addEventListener(name, () => refreshNow());
document.addEventListener("visibilitychange", () => {
  if (visibleNow()) { post({ type: "visible" }); refreshNow(); }
});
// A visible tab that doesn't poll tells the polling tab it is being looked at, once per interval.
(function lookingPing() {
  if (visibleNow() && !leader) post({ type: "visible" });
  setTimeout(lookingPing, pollAfter * 1000);
}());

// A new release: reload at a quiet moment, never with a dialog open or a save pending (design 7.1).
function checkVersion(version) {
  if (version && meta.dataset.appVersion && version !== meta.dataset.appVersion) reloadWanted = true;
  if (reloadWanted && quiet()) window.location.reload();
}
const quiet = () => !document.querySelector("dialog[open]") && !acting.size && !pendingList().length && !hot();

// --- applying a fragment without moving rows under the finger (design 7.4) ---

let lastActivity = 0;
let lastPointer = "mouse";
let deferred = null;
let deferredSince = 0;
let pointerX = -1;
let pointerY = -1;
for (const name of ["pointermove", "pointerdown", "wheel", "keydown", "touchstart"]) {
  for (const area of [card, strip]) {  // the strip's rows don't move under the finger either
    if (area) area.addEventListener(name, (event) => {
      lastActivity = Date.now();
      if (event.pointerType) lastPointer = event.pointerType;
      if (name === "touchstart") lastPointer = "touch";
      if (event.clientX != null) { pointerX = event.clientX; pointerY = event.clientY; }
      else if (event.touches && event.touches[0]) { pointerX = event.touches[0].clientX; pointerY = event.touches[0].clientY; }
    }, { passive: true });
  }
}

// --- rows stay where the eye and the finger are (M1 decision R8) ---
// Before a patch, the anchor is the row under the last pointer, else the row with focus, else the first
// fully visible row; after it, the page scrolls by however far that row moved. Any row whose place on
// screen or whose buttons changed is stamped, and a tap on it within 700 ms is ignored and the row flashes.

const MOVED_MS = 700;
const movedAt = new Map();  // id -> time its position or its buttons last changed
const actsOf = (row) => [...row.querySelectorAll("[data-act]")].map((b) => b.dataset.act).join(",");

function snapshot() {
  const rows = [...list.querySelectorAll(".appt")];
  const tops = new Map(rows.map((row) => [row.dataset.id, { top: row.getBoundingClientRect().top, acts: actsOf(row) }]));
  let anchor = null;
  if (pointerX >= 0) {
    const under = document.elementFromPoint(pointerX, pointerY);
    anchor = under && under.closest && under.closest(".appt");
  }
  if (!anchor && document.activeElement) anchor = document.activeElement.closest && document.activeElement.closest(".appt");
  if (!anchor) anchor = rows.find((row) => { const r = row.getBoundingClientRect(); return r.height && r.top >= 0 && r.bottom <= window.innerHeight; });
  return { tops, anchorId: anchor ? anchor.dataset.id : null, anchorTop: anchor ? anchor.getBoundingClientRect().top : 0 };
}

function settle(before) {
  if (before.anchorId) {
    const anchor = list.querySelector(`.appt[data-id="${before.anchorId}"]`);
    if (anchor) {
      const delta = anchor.getBoundingClientRect().top - before.anchorTop;
      if (Math.abs(delta) >= 1) {
        window.scrollBy(0, delta);  // narrow screens: the window scrolls
        const main = document.getElementById("main");  // wide screens: the page box scrolls
        if (main) main.scrollBy(0, delta);
      }
    }
  }
  const now = Date.now();
  list.querySelectorAll(".appt").forEach((row) => {
    const was = before.tops.get(row.dataset.id);
    if (!was || Math.abs(row.getBoundingClientRect().top - was.top) >= 2 || was.acts !== actsOf(row)) movedAt.set(row.dataset.id, now);
  });
}

function tooSoon(row) {
  if (Date.now() - (movedAt.get(row.dataset.id) || 0) >= MOVED_MS) return false;
  row.classList.remove("flash");
  void row.offsetWidth;  // restart the animation
  row.classList.add("flash");
  return true;
}
window.addEventListener("scroll", () => { lastActivity = Date.now(); }, { passive: true, capture: true });  // capture: the page box's scroll too
card.addEventListener("pointerleave", () => { lastActivity = 0; flushDeferred(); setTimeout(flushHeld, 0); });
// A row held because the mouse was on it is updated once the mouse moves off that row (Instinct review, 2026-11-21).
card.addEventListener("pointerout", (event) => {
  const row = event.target.closest && event.target.closest(".appt");
  if (row && !row.contains(event.relatedTarget) && heldRows.has(row.dataset.id)) setTimeout(flushHeld, 0);
});
if (strip) strip.addEventListener("pointerleave", () => { lastActivity = 0; flushDeferred(); });
card.addEventListener("focusout", () => setTimeout(flushHeld, 0));
card.addEventListener("toggle", (event) => {
  if (!event.target.open) { setTimeout(flushHeld, 0); return; }
  // "Didn't come" shows once the booking's time has passed, by the server's clock.
  event.target.querySelectorAll("[data-act='no-show'][data-after]").forEach((item) => {
    const after = Date.parse(item.dataset.after);
    item.hidden = !Number.isNaN(after) && after > Date.now() + clockSkew;
  });
}, true);

function hot() {
  const window_ = lastPointer === "mouse" ? 1500 : 2500;
  return Date.now() - lastActivity < window_;
}

function held(row) {
  if (acting.has(`${row.dataset.id}:busy`)) return true;
  if (mine.has(row.dataset.id)) return false;
  if (row.querySelector("details[open]")) return true;
  if (row.querySelector(":focus-visible")) return true;
  return lastPointer === "mouse" && row.matches(":hover");
}

function flushDeferred() {
  if (deferred && !hot()) { const html = deferred; deferred = null; apply(html); }
}
setInterval(() => { flushDeferred(); if (heldRows.size) flushHeld(); }, 700);

function flushHeld() {
  const before = heldRows.size ? snapshot() : null;
  for (const [id, html] of heldRows) {
    const row = list.querySelector(`.appt[data-id="${id}"]`);
    if (!row) { heldRows.delete(id); continue; }
    if (held(row)) continue;
    heldRows.delete(id);
    replaceRow(row, html);
  }
  stats();
  if (before) settle(before);
}

function replaceRow(row, html) {
  const fresh = parseFragment(html).firstElementChild;
  const focusedAct = row.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.act : null;
  row.replaceWith(fresh);
  rowHtml.set(fresh.dataset.id, html);
  markActing(fresh);
  applyFilters(fresh);
  if (focusedAct) {
    const again = fresh.querySelector(`[data-act="${focusedAct}"]`) || fresh.querySelector("a, button");
    if (again) again.focus({ preventScroll: true });
  }
  return fresh;
}

function apply(html) {
  if (!mine.size && hot() && !(deferredSince && Date.now() - deferredSince > 10000)) {  // own change: at once
    if (!deferred) deferredSince = Date.now();
    deferred = html;
    return;
  }
  deferred = null;
  deferredSince = 0;
  const root = parseFragment(html).querySelector("[data-queue]");
  if (!root) return;
  const before = snapshot();
  for (const name of ["doctors", "money", "truncated"]) {
    const fresh = root.querySelector(`[data-slot="${name}"]`);
    const live = document.querySelector(`[data-slot="${name}"]`);
    if (!fresh || !live || live.dataset.html === fresh.innerHTML) continue;
    if (live.contains(document.activeElement) && name === "doctors") continue;  // keep the chip under focus
    live.dataset.html = fresh.innerHTML;
    live.replaceChildren(...fresh.childNodes);
  }
  patchStrip(root.querySelector('[data-slot="followups"]'));
  patchGroups(root.querySelector('[data-slot="groups"]'));
  card.dataset.version = root.dataset.version;
  applyFilters();
  stats();
  settle(before);
}

// The strip is replaced whole, but never while one of its rows is saving: that answer brings a new fragment.
function patchStrip(fresh) {
  if (!strip || !fresh || strip.dataset.html === fresh.innerHTML || strip.querySelector("[data-busy]")) return;
  const focused = strip.contains(document.activeElement) ? document.activeElement.closest("[data-fu]") : null;
  strip.dataset.html = fresh.innerHTML;
  strip.replaceChildren(...fresh.childNodes);
  const again = focused && strip.querySelector(`[data-fu][data-id="${focused.dataset.id}"][data-doctor="${focused.dataset.doctor}"] button`);
  if (again) again.focus({ preventScroll: true });
}

function patchGroups(fresh) {
  const freshGroups = [...fresh.querySelectorAll(":scope > .queue-group")];
  const liveGroups = [...list.querySelectorAll(":scope > .queue-group")];
  if (!freshGroups.length || !liveGroups.length) {  // an empty day, or the first doctor's sessions
    if ([...list.querySelectorAll(".appt")].some(held)) return;
    list.replaceChildren(...fresh.childNodes);
    rememberRows();
    return;
  }
  const keep = new Set(freshGroups.map((g) => g.dataset.doctor));
  for (const group of liveGroups) {
    if (!keep.has(group.dataset.doctor) && ![...group.querySelectorAll(".appt")].some(held)) group.remove();
  }
  list.querySelectorAll(":scope > .empty-note").forEach((note) => note.remove());
  let prev = null;
  for (const freshGroup of freshGroups) {
    let group = list.querySelector(`:scope > .queue-group[data-doctor="${freshGroup.dataset.doctor}"]`);
    if (!group) {
      group = freshGroup;
      if (prev) prev.after(group); else list.prepend(group);
      group.querySelectorAll(".appt").forEach((row) => rowHtml.set(row.dataset.id, row.outerHTML));
    } else {
      const head = group.querySelector("[data-head]");
      const freshHead = freshGroup.querySelector("[data-head]");
      if (head.outerHTML !== freshHead.outerHTML) head.replaceWith(freshHead);
      for (const name of ["open", "booked", "done"]) {
        const live = group.querySelector(`[data-rows="${name}"]`);
        const fresh = freshGroup.querySelector(`[data-rows="${name}"]`);
        if (live && fresh) patchRows(live, fresh);
      }
      const fold = group.querySelector("[data-fold]");
      const freshFold = freshGroup.querySelector("[data-fold]");
      if (fold && freshFold) {  // the count and whether it shows; open or closed stays as the person left it
        fold.hidden = freshFold.hidden;
        fold.querySelector("[data-fold-count]").textContent = freshFold.querySelector("[data-fold-count]").textContent;
      }
      const booked = group.querySelector("[data-booked-wrap]");
      const freshBooked = freshGroup.querySelector("[data-booked-wrap]");
      if (booked && freshBooked) {
        booked.hidden = freshBooked.hidden;
        booked.querySelector("[data-booked-count]").textContent = freshBooked.querySelector("[data-booked-count]").textContent;
      }
      group.querySelector("[data-empty]").hidden = freshGroup.querySelector("[data-empty]").hidden;
      if ((prev ? prev.nextElementSibling : list.firstElementChild) !== group) {
        if (prev) prev.after(group); else list.prepend(group);
      }
    }
    prev = group;
  }
}

function patchRows(rows, freshRows) {
  const fresh = [...freshRows.children];
  const ids = new Set(fresh.map((row) => row.dataset.id));
  for (const row of [...rows.children]) {
    if (ids.has(row.dataset.id)) continue;
    if (held(row)) row.classList.add("gone");
    else { row.remove(); rowHtml.delete(row.dataset.id); heldRows.delete(row.dataset.id); }
  }
  let prev = null;
  for (const freshRow of fresh) {
    const id = freshRow.dataset.id;
    const html = freshRow.outerHTML;
    let row = rows.querySelector(`:scope > .appt[data-id="${id}"]`);
    if (!row) {
      const elsewhere = list.querySelector(`.appt[data-id="${id}"]`);  // moved to another doctor
      if (elsewhere && !held(elsewhere)) elsewhere.remove();
      row = freshRow;
      if (prev) prev.after(row); else rows.prepend(row);
      rowHtml.set(id, html);
      markActing(row);
    } else if (rowHtml.get(id) !== html) {
      if (held(row)) heldRows.set(id, html);
      else row = replaceRow(row, html);
    }
    if (!held(row) && (prev ? prev.nextElementSibling : rows.firstElementChild) !== row) {
      if (prev) prev.after(row); else rows.prepend(row);
    }
    prev = row;
  }
}

function rememberRows() {
  list.querySelectorAll(".appt").forEach((row) => { rowHtml.set(row.dataset.id, row.outerHTML); markActing(row); });
}

// --- how long each patient has waited, from arrival, by the server's clock (Saged, 2026-11-13) ---
// The fragment only changes when the day does, so the minutes tick here; web/format.py wait_text says the same.

function waitText(minutes) {
  const m = Math.max(0, Math.floor(minutes));
  const h = Math.floor(m / 60);
  const rest = m % 60;
  const say = (key) => t(key, { h: localDigits(h), m: localDigits(rest) });
  if (m >= 240) return t("wait_over", { h: localDigits(4) });
  if (m < 1) return t("wait_now");
  if (!h) return say("wait_m");
  if (h === 1) return say(rest ? "wait_1h_m" : "wait_1h");
  return say(rest ? "wait_h_m" : "wait_h");
}

function tickWaits() {
  const now = Date.now() + clockSkew;
  list.querySelectorAll("[data-wait-since]").forEach((node) => {
    const since = Date.parse(node.dataset.waitSince);
    if (Number.isNaN(since)) return;
    const text = waitText((now - since) / 60000);
    if (node.textContent !== text) node.textContent = text;
  });
}
setInterval(tickWaits, 30000);

// --- filters by doctor and by section (design D3), and the counts ---

const filterKey = `hamada.today.filter.${membership()}`;
let filter = { doctor: "", section: "" };
try { filter = { ...filter, ...JSON.parse(localStorage.getItem(filterKey) || "{}") }; } catch (e) { /* none */ }
if (!localStorage.getItem(filterKey) && config.me.role === "doctor" && list.querySelector(`.queue-group[data-doctor="${config.me.membership_id}"]`)) {
  filter.doctor = String(config.me.membership_id);  // a doctor opens on their own queue
}

function applyFilters(only) {
  const groups = only ? [] : list.querySelectorAll(":scope > .queue-group");
  if (filter.doctor && !list.querySelector(`.queue-group[data-doctor="${filter.doctor}"]`)) filter.doctor = "";
  (only ? [only] : list.querySelectorAll(".appt")).forEach((row) => {
    row.hidden = Boolean(filter.section) && Boolean(row.dataset.section) && row.dataset.section !== filter.section;
  });
  // A doctor's group shows when it matches the doctor filter and, in a center, works in the chosen section.
  groups.forEach((group) => {
    const inSection = !filter.section || (group.dataset.sections || "").split(",").includes(filter.section)
      || Boolean(group.querySelector(".appt:not([hidden])"));
    group.hidden = (Boolean(filter.doctor) && group.dataset.doctor !== filter.doctor) || !inSection;
  });
  document.querySelectorAll("[data-filter-doctor]").forEach((chip) => {
    const group = chip.dataset.filterDoctor && list.querySelector(`.queue-group[data-doctor="${chip.dataset.filterDoctor}"]`);
    chip.hidden = Boolean(filter.section && group && !(group.dataset.sections || "").split(",").includes(filter.section));
  });
  document.querySelectorAll("[data-filter-doctor]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filterDoctor === filter.doctor)));
  document.querySelectorAll("[data-filter-section]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filterSection === filter.section)));
}

document.addEventListener("click", (event) => {
  const doctor = event.target.closest("[data-filter-doctor]");
  const section = event.target.closest("[data-filter-section]");
  if (!doctor && !section) return;
  if (doctor) filter.doctor = doctor.dataset.filterDoctor;
  if (section) filter.section = section.dataset.filterSection;
  try { localStorage.setItem(filterKey, JSON.stringify(filter)); } catch (e) { /* none */ }
  applyFilters();
  stats();
});

function stats() {
  const counts = { total: 0, waiting: 0, in_room: 0, done: 0 };
  list.querySelectorAll(".queue-group:not([hidden]) .appt:not([hidden]):not(.gone)").forEach((row) => {
    const status = row.dataset.status;
    if (status !== "cancelled") counts.total += 1;
    if (status in counts) counts[status] += 1;
  });
  for (const [key, value] of Object.entries(counts)) document.querySelector(`[data-stat="${key}"]`).textContent = localDigits(value);
}

// --- row actions: one tap, one request, one key; a double tap does nothing ---

const visitDialogs = initVisitDialogs({ onChanged: changed });
const pay = initPay({ onChanged: changed });
const book = initBook({
  onBooked: changed,
  defaults: () => ({ doctorId: filter.doctor || "", sectionId: filter.section || "" }),
});

function changed() { refreshNow(); post({ type: "poll-now" }); }

function visitOf(row) {
  const name = row.dataset.name;
  const service = row.querySelector(".what bdi");
  return {
    id: Number(row.dataset.id), status: row.dataset.status, name, due: Number(row.dataset.due), fee: Number(row.dataset.fee),
    paid: Number(row.dataset.paid) || 0, earlier: Number(row.dataset.earlier) || 0,
    patient: { id: Number(row.dataset.patient), name, file: row.dataset.file, phone: row.dataset.phone || "",
               owed: Number(row.dataset.earlier) || 0 },
    invoiceId: row.dataset.invoice ? Number(row.dataset.invoice) : null, doctorId: Number(row.dataset.doctor),
    day: row.dataset.day, service: service ? service.textContent : "",
  };
}

function markActing(row) {
  const busy = [...acting.keys()].some((key) => key.startsWith(`${row.dataset.id}:`));
  row.classList.toggle("saving", busy);
  row.querySelectorAll("[data-act]").forEach((b) => { if (busy) b.disabled = true; });
}

const MESSAGES = {
  arrive: "arrived", call: "called", finish: "finished", "undo-arrive": "undone_arrive", "undo-call": "undone_call",
  ready: "readied", "undo-ready": "undone_ready", wait: "told_wait", "no-show": "marked_no_show", "undo-no-show": "undone_no_show",
};
const UNDO = { arrive: ["undo-arrive", "waiting"], call: ["undo-call", "in_room"], ready: ["undo-ready", "ready"],
               "no-show": ["undo-no-show", "no_show"] };
const UNDO_FLAG = { "undo-arrive": "undoArrive", "undo-call": "undoCall" };  // the others always exist

async function quick(id, action, status, name, doctorId, extra = null) {
  const key = `${id}:${action}`;
  if (acting.has(key)) return;
  acting.set(key, true);
  acting.set(`${id}:busy`, true);
  const row = list.querySelector(`.appt[data-id="${id}"]`);
  if (row) markActing(row);
  // auto: the body carries the expected status, so a resend at the next page load is harmless (R7).
  const result = await save("POST", `/api/v1/appointments/${id}/${action}`, { status, ...extra }, { label: t(MESSAGES[action], { name }), auto: true });
  acting.delete(key);
  acting.delete(`${id}:busy`);
  if (result.settled && isOk(result)) {
    mine.add(String(id));
    setTimeout(() => mine.delete(String(id)), 10000);  // long enough for the next poll's answer
  }
  const now = list.querySelector(`.appt[data-id="${id}"]`);
  if (now) { now.classList.remove("saving"); now.querySelectorAll("[data-act]").forEach((b) => { b.disabled = false; }); }
  if (!result.settled) { renderPending(); return; }
  if (isOk(result) && action === "call" && config.me.treats && doctorId === config.me.membership_id) {
    window.location.assign(`/visits/${id}`);  // the doctor's own call-in opens the visit screen; the desk stays
    return;
  }
  const error = result.data && result.data.error;
  if (action === "ready" && error && error.code === "doctor_asked") {
    // One request to a doctor at a time (Saged, 2026-11-24): take the other one back, and ask about this one.
    toast(error.message, { label: t("ask_instead", { name }), onClick: async () => {
      await quick(error.appointment_id, "undo-ready", "ready", error.name);
      quick(id, "ready", status, name, doctorId);
    } });
    changed();
    return;
  }
  if (isOk(result)) {
    const undo = UNDO[action];
    const canUndo = undo && (!UNDO_FLAG[undo[0]] || card.dataset[UNDO_FLAG[undo[0]]] === "1");
    toast(t(MESSAGES[action], { name }), canUndo ? { label: t("undo"), onClick: () => quick(id, undo[0], undo[1], name) } : null);
  } else {
    alertMessage(errorMessage(result, t("failed")));
  }
  changed();
}

card.addEventListener("click", (event) => {
  const button = event.target.closest("[data-act]");
  if (!button || button.disabled) return;
  const row = button.closest(".appt");
  if (tooSoon(row)) { event.preventDefault(); return; }  // the row just moved or changed under the finger
  const menu = button.closest("details");
  if (menu) menu.open = false;
  const v = visitOf(row);
  switch (button.dataset.act) {
    case "arrive": case "call": case "ready": case "wait": case "undo-arrive": case "undo-call": case "no-show": case "undo-no-show":
      quick(v.id, button.dataset.act, v.status, v.name, v.doctorId);
      break;
    case "next":  // the next visit of a plan: the same patient and doctor, no search (desk review 4)
      if (book) book.open({ patient: v.patient, doctorId: v.doctorId, opener: button, continues: v.id });
      break;
    case "finish": {
      // Someone other than the visit's doctor finishing it (Saged, 2026-11-24): asked first, and the server checks
      // that the doctor wrote what was done and the notes or the chart, as for the doctor's own finish.
      const forDoctor = v.status === "in_room" && v.doctorId !== config.me.membership_id;
      if (forDoctor && !window.confirm(t("desk_finish_confirm", { name: v.name }))) break;
      v.checkRecord = forDoctor;
      if (config.perms.collect && (v.due > 0 || v.earlier > 0)) visitDialogs.finish(v, button);
      else quick(v.id, "finish", v.status, v.name, v.doctorId, forDoctor ? { check_record: true } : null);
      break;
    }
    case "pay": case "invoice":
      if (pay) pay.open(v);
      break;
    case "receipt":
      if (v.invoiceId) window.open(receiptUrl(v.invoiceId), "_blank");
      break;
    case "reschedule": visitDialogs.move(v, button); break;
    case "cancel": visitDialogs.cancel(v, button); break;
    default: break;
  }
});

// --- "just out of the visit": book the plan's sessions now, or later from Follow-ups ---

if (strip) {
  strip.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-fu]");
    if (!row) return;
    const opener = event.target.closest("[data-fu-book]");
    if (opener && book) openPlan(book, row, opener);
    if (event.target.closest("[data-fu-later]") && await later(row, true)) { row.hidden = true; changed(); }
  });
}

// --- the Book and Walk-in buttons at the top ---

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-open-book]");
  if (opener && book) book.open({ opener, fixedDoctor: filter.doctor || null });
  const walkOpener = event.target.closest("[data-open-walkin]");
  if (walkOpener && walkIn) walkIn.open({ opener: walkOpener, doctorId: filter.doctor || "" });
});

const walkIn = initWalkIn({ onBooked: changed });
showReopen();  // "Back to the visit" for five minutes after a finish (Saged, 2026-11-24)

// --- start ---

document.querySelectorAll("[data-slot]").forEach((slot) => { if (slot.dataset.slot !== "groups") slot.dataset.html = slot.innerHTML; });
rememberRows();
applyFilters();
stats();
showFreshness();
window.addEventListener("hamada:pending-settled", () => changed());
