// Every action goes through here (design 7.1, 7.4).
// - A form's request key lives until a definite answer: a 2xx, or a 4xx that wrote nothing.
//   409 `busy` and 503 are never definite: the same key is resent after Retry-After.
// - The pending request is kept in sessionStorage, so a reload or a sign-in settles it first.
//   A pending request older than 24 hours is never resent.
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const jitter = (ms) => Math.round(ms * (0.8 + Math.random() * 0.4));

async function once(method, url, body, key) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();
  if (membership()) headers["X-Membership"] = membership();
  if (key) headers["Idempotency-Key"] = key;
  try {
    const response = await fetch(url, {
      method, headers, credentials: "same-origin", signal: controller.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    return { status: response.status, data, retryAfter: Number(response.headers.get("Retry-After")) || 0 };
  } catch (error) {
    return { status: 0, data: {}, retryAfter: 0 };  // network error or timeout: outcome unknown
  } finally {
    clearTimeout(timer);
  }
}

function retryable(result) {
  const code = result.data && result.data.error && result.data.error.code;
  return result.status === 0 || [502, 503, 504, 429].includes(result.status) || (result.status === 409 && code === "busy");
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
    if (result.status === 401) {
      window.dispatchEvent(new CustomEvent("hamada:signed-out"));
      return { ...result, settled: true };
    }
    if (result.status === 409 && code === "clinic_changed") {
      window.location.reload();
      return { ...result, settled: true };
    }
    if (!retryable(result)) return { ...result, settled: true };
    attempt += 1;
    if (Date.now() - started > GIVE_UP_MS) return { ...result, settled: false };
    if (onWaiting) onWaiting(attempt);
    const backoff = result.retryAfter ? result.retryAfter * 1000 : Math.random() * Math.min(8000, 500 * 2 ** attempt);
    await sleep(jitter(backoff));
  }
}

export const get = (url) => send("GET", url);

// --- Pending saves (the browser half of safe repeats) ---

function readPending() {
  try { return JSON.parse(sessionStorage.getItem(PENDING) || "null"); } catch (e) { return null; }
}
function writePending(value) {
  try {
    if (value) sessionStorage.setItem(PENDING, JSON.stringify(value));
    else sessionStorage.removeItem(PENDING);
  } catch (e) { /* private mode: nothing to keep */ }
}
export function clearPending() { writePending(null); }

// Run one action for a form, owning its key until a definite answer.
export async function save(method, url, body, { key, onWaiting } = {}) {
  const pending = { url, method, body, key: key || newKey(), user: userId(), membership: membership(), created_at: Date.now() };
  writePending(pending);
  const result = await send(method, url, body, { key: pending.key, onWaiting });
  if (result.settled) writePending(null);
  return { ...result, key: pending.key };
}

// After a reload or a new sign-in: settle a save whose answer was lost.
export async function settlePending() {
  const pending = readPending();
  if (!pending) return null;
  if (pending.user !== userId() || pending.membership !== membership()) { writePending(null); return null; }
  if (Date.now() - pending.created_at > DAY_MS) {
    writePending(null);
    return { status: 0, settled: false, expired: true };
  }
  const result = await send(pending.method, pending.url, pending.body, { key: pending.key });
  if (result.settled) writePending(null);
  return result;
}

export function errorMessage(result, fallback) {
  if (result.data && result.data.error && result.data.error.message) return result.data.error.message;
  return fallback;
}
