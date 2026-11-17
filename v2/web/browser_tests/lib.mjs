// Shared helpers for the browser tests: headless Edge over the DevTools protocol, sign-in, and the JSON API.
// Node 24 or later (built-in fetch and WebSocket). No packages to install.
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const BASE = process.env.HAMADA_BT_BASE || "http://127.0.0.1:8003";
export const USER = process.env.HAMADA_BT_USER || "sara";
export const PASSWORD = process.env.HAMADA_BT_PASSWORD || "Hamada-Demo-2026";
export const CLINIC = process.env.HAMADA_BT_CLINIC || "سمايل";  // part of the clinic's name, for people in several
const EDGE = process.env.HAMADA_BT_EDGE || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function check(ok, message) {
  if (!ok) throw new Error(`FAILED: ${message}`);
  console.log(`  ok: ${message}`);
}

// --- signing in and the API, as the browser does it (CSRF cookie, X-Membership, a request key) ---

export async function signIn(user = USER) {
  const jar = {};
  const keep = (r) => { for (const c of r.headers.getSetCookie()) { const [kv] = c.split(";"); const i = kv.indexOf("="); jar[kv.slice(0, i)] = kv.slice(i + 1); } };
  const cookie = () => Object.entries(jar).map(([k, v]) => `${k}=${v}`).join("; ");
  keep(await fetch(BASE + "/login"));
  const headers = (extra = {}) => ({ "Content-Type": "application/json", Cookie: cookie(), "X-CSRFToken": jar.hamada_csrftoken || "", Origin: BASE, ...extra });
  let r = await fetch(BASE + "/api/v1/auth/login", { method: "POST", headers: headers(), body: JSON.stringify({ username: user, password: PASSWORD }) });
  keep(r);
  if (r.status !== 200) throw new Error(`sign-in failed for ${user}: ${r.status} ${await r.text()}`);
  r = await fetch(BASE + "/api/v1/me", { headers: headers() });
  const me = await r.json();
  const place = me.clinics.find((c) => c.name.includes(CLINIC)) || me.clinics[0];
  r = await fetch(BASE + "/api/v1/me/clinic", { method: "POST", headers: headers({ "Idempotency-Key": randomUUID() }), body: JSON.stringify({ membership_id: place.membership_id }) });
  keep(r);
  if (r.status !== 200) throw new Error(`picking the clinic failed: ${r.status}`);
  const membership = place.membership_id;
  async function api(method, path, body) {
    const h = headers({ "X-Membership": String(membership) });
    if (method !== "GET") h["Idempotency-Key"] = randomUUID();
    const res = await fetch(BASE + path, { method, headers: h, body: method === "GET" ? undefined : JSON.stringify(body || {}) });
    keep(res);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, data };
  }
  return { session: jar.hamada_session, membership, api };
}

// Today's appointments for a patient name, read from the JSON API.
// Tests that read Arabic screen text run the signed-in person in Arabic and give them their own language back after:
// `const back = await inArabic(api); try { ... } finally { await back(); }`. It changes no permission.
export async function inArabic(api) {
  const before = (await api("GET", "/api/v1/me")).data.user.language;
  if (before !== "ar") await api("POST", "/api/v1/me/language", { language: "ar" });
  return async () => { if (before !== "ar") await api("POST", "/api/v1/me/language", { language: before }); };
}

export async function bookingsOf(api, name) {
  const today = (await api("GET", "/api/v1/today")).data;
  return today.queues.flatMap((q) => q.appointments).filter((a) => a.patient.name === name && a.status !== "cancelled");
}

// A doctor has one patient in the room at a time (Saged, 2026-11-13): finish whoever of theirs is in the room
// before a test calls someone in for them. Development data only.
export async function freeRoom(api, doctorId) {
  const today = (await api("GET", "/api/v1/today")).data;
  for (const a of today.queues.flatMap((q) => q.appointments)) {
    if (a.status === "in_room" && a.doctor_id === doctorId) await api("POST", `/api/v1/appointments/${a.id}/finish`, { status: "in_room" });
  }
}

// --- the browser ---

export async function browser({ width = 1280, height = 900, touch = false } = {}) {
  const port = 9300 + Math.floor(Math.random() * 600);
  const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "hamada-bt-"))}`,
    "--hide-scrollbars", "--no-first-run", "--disable-gpu", `--window-size=${width},${height}`, "about:blank"], { stdio: "ignore" });
  let target;
  for (let i = 0; i < 60 && !target; i++) {
    await sleep(200);
    try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page"); } catch (e) { /* starting */ }
  }
  if (!target) throw new Error("headless Edge didn't start");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r, { once: true }));
  let id = 0;
  const waiting = new Map();
  const listeners = [];
  const events = [];
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); return; }
    if (m.method) { events.push(m); listeners.forEach((fn) => fn(m)); }
  });
  const cdp = (method, params = {}) => new Promise((r) => { const n = ++id; waiting.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
  await cdp("Network.enable");
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: touch });
  if (touch) await cdp("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });

  const page = {
    cdp,
    on: (fn) => listeners.push(fn),
    async cookie(session) {
      await cdp("Network.setCookie", { name: "hamada_session", value: session, url: BASE, httpOnly: true });
    },
    async open(path) {
      events.length = 0;
      await cdp("Page.navigate", { url: BASE + path });
      for (let i = 0; i < 150 && !events.some((e) => e.method === "Page.loadEventFired"); i++) await sleep(100);
      await sleep(800);
    },
    async eval(expression) {
      const r = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 600));
      return r.result && r.result.result ? r.result.result.value : undefined;
    },
    // Wait until a page expression is truthy.
    async until(expression, ms = 15000, what = expression) {
      const end = Date.now() + ms;
      while (Date.now() < end) {
        if (await page.eval(expression)) return true;
        await sleep(150);
      }
      throw new Error(`timed out waiting for: ${what}`);
    },
    // A real tap or click at a point, as the input pipeline sends it.
    async tap(x, y) {
      if (touch) {
        await cdp("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
        await cdp("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      } else {
        await cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
        await cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
        await cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      }
    },
    errors: () => events.filter((e) => e.method === "Runtime.exceptionThrown").map((e) => JSON.stringify(e.params.exceptionDetails).slice(0, 400)),
    close() { try { ws.close(); } catch (e) { /* closed */ } edge.kill(); },
  };
  return page;
}
