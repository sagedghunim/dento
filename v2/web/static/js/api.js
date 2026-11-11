// Every action goes through here (design 7.1, 7.4).
// - A form's request key lives until a definite answer: a 2xx, or a 4xx that wrote nothing.
//   409 `busy` and 503 are never definite: the same key is resent after Retry-After.
// - Pending requests are kept in sessionStorage, so a reload or a sign-in settles them first.
//   Several can be pending at once (one per row action). One older than 24 hours is never resent.
// - Retries only after network errors, timeouts, 502/503/504, 409 busy and 429. Never after a 500.

const meta = document.querySelector('meta[name="hamada"]');
const PENDING = "hamada.pending";
const TIMEOUT_MS = 10000;
const GIVE_UP_MS = 20000;
const DAY_MS = 24 * 3600 * 1000;

export const membership = () => (meta && meta.dataset.membership) || "";
const userId = () => (meta && meta.dataset.user) || "";

export function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // Plain http on a LAN has no randomUUID (design 7.4).
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function csrfToken() {
  const name = (meta && meta.dataset.csrfCookie) || "hamada_csrftoken";
  const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : "";
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const jitter = (ms) => Math.round(ms * (0.8 + Math.random() * 0.4));

function headersFor(method, body, key, extra) {
  const headers = { Accept: "application/json", ...(extra || {}) };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();  // read at each send: sign-in rotates it
  if (membership()) headers["X-Membership"] = membership();
  if (key) headers["Idempotency-Key"] = key;
  return headers;
}

async function once(method, url, body, key) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method, headers: headersFor(method, body, key), credentials: "same-origin", signal: controller.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    return {
      status: response.status, data, retryAfter: Number(response.headers.get("Retry-After")) || 0,
      echoed: !key || response.headers.get("Idempotency-Key") === key,
      version: response.headers.get("X-App-Version") || "",
    };
  } catch (error) {
    return { status: 0, data: {}, retryAfter: 0, echoed: false };  // network error or timeout: outcome unknown
  } finally {
    clearTimeout(timer);
  }
}

function retryable(result) {
  const code = result.data && result.data.error && result.data.error.code;
  // A 429 that asks for minutes (sign-in throttles) wrote nothing: say so now rather than wait.
  if (result.status === 429) return result.retryAfter > 0 && result.retryAfter <= 10;
  return result.status === 0 || [502, 503, 504].includes(result.status) || (result.status === 409 && code === "busy");
}

// Send an action. Returns {status, data, settled}. settled=false means the outcome is unknown.
export async function send(method, url, body, { key, onWaiting } = {}) {
  const started = Date.now();
  let attempt = 0;
  let csrfRetried = false;
  for (;;) {
    const result = await once(method, url, body, key);
    const code = result.data && result.data.error && result.data.error.code;
    if (result.status === 403 && code === "csrf_failed" && !csrfRetried) {
      csrfRetried = true;   // the token changed at sign-in: read the cookie again and resend once
      continue;
    }
    if (result.status === 401 && code === "signed_out" && key) {
      // Nothing was written. The request stays pending and is settled after the person signs in again.
      window.dispatchEvent(new CustomEvent("hamada:signed-out"));
      return { ...result, settled: false, signedOut: true };
    }
    if (result.status === 401 && code === "signed_out") {
      window.dispatchEvent(new CustomEvent("hamada:signed-out"));
      return { ...result, settled: true, signedOut: true };
    }
    if (result.status === 409 && code === "clinic_changed") {
      window.location.reload();
      return { ...result, settled: true };
    }
    // A 2xx without our key echoed back isn't a confirmation of this request (design 7.4).
    if (result.status >= 200 && result.status < 300 && !result.echoed) return { ...result, settled: false };
    if (!retryable(result)) return { ...result, settled: true };
    attempt += 1;
    const left = GIVE_UP_MS - (Date.now() - started);
    if (left <= 0) return { ...result, settled: false };
    if (onWaiting) onWaiting(attempt);
    const backoff = result.retryAfter ? jitter(result.retryAfter * 1000) : Math.random() * Math.min(8000, 500 * 2 ** attempt);
    await sleep(Math.min(backoff, left));
  }
}

export const get = (url) => send("GET", url);

// A same-origin HTML fragment (design 7.1): {status, text, headers}. status 0 is a network error or timeout.
export async function fetchText(url, { etag, signal, timeout = TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
  const headers = headersFor("GET", undefined, undefined, { Accept: "text/html" });
  if (etag) headers["If-None-Match"] = etag;
  try {
    const response = await fetch(url, { headers, credentials: "same-origin", signal: controller.signal, cache: "no-store" });
    const text = response.status === 200 ? await response.text() : "";
    let data = {};
    if (response.status >= 400) { try { data = JSON.parse(await response.text()); } catch (e) { data = {}; } }
    return { status: response.status, text, data, headers: response.headers };
  } catch (error) {
    return { status: 0, text: "", data: {}, headers: new Headers(), aborted: Boolean(signal && signal.aborted) };
  } finally {
    clearTimeout(timer);
  }
}

// --- Pending saves (the browser half of safe repeats) ---

function readPending() {
  try {
    const value = JSON.parse(sessionStorage.getItem(PENDING) || "[]");
    return Array.isArray(value) ? value : value ? [value] : [];
  } catch (e) { return []; }
}
function writePending(list) {
  try {
    if (list.length) sessionStorage.setItem(PENDING, JSON.stringify(list));
    else sessionStorage.removeItem(PENDING);
  } catch (e) { /* private mode: nothing to keep */ }
}
function forget(key) { writePending(readPending().filter((p) => p.key !== key)); }
export function clearPending() { writePending([]); }
export function discardPending(key) { forget(key); }
export function pendingList() {
  return readPending().filter((p) => p.user === userId() && p.membership === membership());
}

// Run one action, owning its key until a definite answer. `label` names it for the "not confirmed" list.
export async function save(method, url, body, { key, onWaiting, label } = {}) {
  const entry = { url, method, body, key: key || newKey(), label: label || "", user: userId(),
                  membership: membership(), created_at: Date.now() };
  writePending([...readPending().filter((p) => p.key !== entry.key), entry]);
  const result = await send(method, url, body, { key: entry.key, onWaiting });
  if (result.settled) forget(entry.key);
  return { ...result, key: entry.key };
}

// Resend a pending request with the same key and body.
export async function retryPending(key, { onWaiting } = {}) {
  const entry = readPending().find((p) => p.key === key);
  if (!entry) return null;
  if (Date.now() - entry.created_at > DAY_MS) { forget(key); return { status: 0, settled: false, expired: true }; }
  const result = await send(entry.method, entry.url, entry.body, { key, onWaiting });
  if (result.settled) forget(key);
  return result;
}

// After a reload or a new sign-in: settle saves whose answers were lost, for this person and clinic only.
export async function settlePending() {
  const all = readPending();
  if (!userId()) return [];  // the sign-in page: keep them for whoever signs in next
  const others = all.filter((p) => p.user !== userId());
  if (others.length) writePending(all.filter((p) => p.user === userId()));  // another person: never replayed
  // Only in the clinic they were made in; another clinic's wait until the person goes back to it.
  const mine = all.filter((p) => p.user === userId() && p.membership === membership());
  const results = [];
  for (const entry of mine) {
    if (Date.now() - entry.created_at > DAY_MS) {
      forget(entry.key);
      results.push({ entry, status: 0, settled: false, expired: true });
      continue;
    }
    const result = await send(entry.method, entry.url, entry.body, { key: entry.key });
    if (result.settled) forget(entry.key);
    results.push({ entry, ...result });
  }
  return results;
}

export function errorMessage(result, fallback) {
  if (result.data && result.data.error && result.data.error.message) return result.data.error.message;
  return fallback;
}

export const isOk = (result) => result.status >= 200 && result.status < 300;
