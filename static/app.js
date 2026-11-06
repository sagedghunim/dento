/* Front end for the Hamada API. The server decides everything; this file only displays it. */
var me = null, current = "today", rx = [], rxData = null;
var $ = function (id) { return document.getElementById(id); };
function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
function egp(p) { var v = p / 100; return "EGP " + v.toLocaleString("en-US", {maximumFractionDigits: 2}); }
function can(p) { return me.perms.indexOf(p) > -1; }
var toastTimer;
function toast(m) { var t = $("toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.classList.remove("show"); }, 3200); }
function cookie(n) { var m = document.cookie.match("(^|; )" + n + "=([^;]*)"); return m ? decodeURIComponent(m[2]) : ""; }
function key() { return Date.now().toString(36) + Math.random().toString(36).slice(2); }

function api(path, body) {
  var o = {credentials: "same-origin", headers: {}};
  if (body !== undefined) {
    o.method = "POST"; o.body = JSON.stringify(body);
    o.headers = {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken"), "Idempotency-Key": key()};
  }
  return fetch(path, o).then(function (r) {
    return r.json().catch(function () { return {error: "Something went wrong. Please try again."}; }).then(function (d) {
      if (r.status === 401 && path !== "/api/login") showLogin();
      if (!r.ok) { var e = new Error(d.error || "Request failed"); e.status = r.status; throw e; }
      return d;
    });
  });
}
/* Run a change, block double clicks on its button, show the result, reload the page. */
function act(path, body, btn) {
  if (btn) btn.disabled = true;
  return api(path, body).then(function (d) { if (d.message) toast(d.message); return render(); })
    .catch(function (e) { toast(e.message); return render(); })
    .finally(function () { if (btn) btn.disabled = false; });
}

var PAGES = {
  today: ["Today at the clinic", "Every booking, who is waiting, and what is happening right now — for the doctor and the assistant, on the same screen."],
  inventory: ["Inventory", "What the clinic bought, what is left, and what each patient used. Stock goes down on its own when use is recorded — nobody recounts shelves."],
  finances: ["Finances", "All of the clinic's money in one place: what came in, and where it went — stock, salaries, rent, electricity and the rest."],
  rx: ["Prescription", "Nothing is written from scratch. Pick from the clinic's ready medicine list, set the amount with the plus and minus buttons, and print."],
  online: ["Online consultations", "Patients who cannot come in can still talk to the doctor. Requests land here, next to the rest of the day's work."],
  team: ["Team & settings", "The doctor owns the clinic's setup: the specialty, who is on the team, and exactly what each person can see and do. Changes apply at once."]
};
function locked(title, text) { return '<div class="locked"><h2>' + esc(title) + '</h2><p>' + esc(text) + '</p></div>'; }
function stockAlert(stock) {
  var low = stock.filter(function (i) { return i.quantity <= i.threshold; }), bar = $("stockAlert");
  bar.classList.toggle("show", low.length > 0);
  bar.innerHTML = low.length ? "<strong>Stock alert:</strong> " + low.map(function (i) { return esc(i.name) + " — only " + i.quantity + " " + esc(i.unit) + " left"; }).join(" · ") + ". Time to reorder." : "";
}

var RENDER = {
  today: function () {
    return api("/api/today").then(function (d) {
      stockAlert(d.stock);
      var meta = {booked:["Booked","grey"],waiting:["Waiting","amber"],room:["In the chair","teal"],done:["Finished","grey"],cancelled:["Cancelled","red"]};
      var acts = {booked:[["checkin","Check in",""],["cancel","Cancel"," danger"]],waiting:[["callin","Call in",""]],room:[["finish","Finish visit",""]]};
      var n = function (s) { return d.appointments.filter(function (a) { return a.status === s; }).length; };
      var total = d.appointments.length - n("cancelled");
      var rows = d.appointments.map(function (a) {
        var buttons = can("booking") ? (acts[a.status] || []).map(function (x) { return '<button class="btn small' + x[2] + '" data-appt="' + a.id + '" data-act="' + x[0] + '">' + x[1] + '</button>'; }).join("") : "";
        return '<div class="appt"><div class="appt-time">' + esc(a.time) + '</div><div class="appt-main"><div class="who">' + esc(a.patient) + (a.isNew ? ' <span class="pill new">New</span>' : "") + '</div><div class="what">' + esc(a.service) + ' · ' + esc(a.doctor) + '</div></div><span class="pill ' + meta[a.status][1] + '">' + meta[a.status][0] + '</span><div class="appt-actions">' + buttons + '</div></div>';
      }).join("") || '<p class="empty-note">No appointments yet today.</p>';
      var form = can("booking") ? '<div class="card"><h2>Add a booking</h2><p class="card-sub">Name, service, time — that is all it takes.</p><form id="bookForm" style="margin-top:12px"><div class="form-row"><label class="field">Patient name<input id="bkName" required maxlength="120" placeholder="e.g. Ahmed Ali" /></label><label class="field">Service<select id="bkService">' + me.clinic.services.map(function (s) { return "<option>" + esc(s) + "</option>"; }).join("") + '</select></label></div><div class="form-row" style="margin-top:10px"><label class="field">Time<input id="bkTime" type="time" value="14:00" required /></label><label class="field">Booked by<select id="bkSource"><option value="clinic">At the clinic</option><option value="phone">By phone</option><option value="online">Online</option></select></label><label class="field">Fee (EGP)<input id="bkFee" type="number" min="0" step="0.5" value="0" required /></label><button class="btn" type="submit">Add booking</button></div></form></div>' : "";
      return '<div class="stats"><div class="stat"><div class="num">' + total + '</div><div class="lbl">Bookings today</div></div><div class="stat"><div class="num">' + n("waiting") + '</div><div class="lbl">Waiting now</div></div><div class="stat"><div class="num">' + n("room") + '</div><div class="lbl">In the chair</div></div><div class="stat"><div class="num">' + n("done") + '</div><div class="lbl">Finished</div></div></div><div class="grid-2"><div class="card"><h2>Today\'s appointments</h2><p class="card-sub">New bookings appear here at once, including online ones.</p><div>' + rows + '</div></div><div class="stack">' + form + '<div class="card"><h2>Latest activity</h2><div>' + (d.feed.map(function (f) { return '<div class="feed-item"><span class="when">' + esc(f.when) + '</span><br />' + esc(f.text) + '</div>'; }).join("") || '<p class="empty-note">Nothing yet today.</p>') + '</div></div></div></div>';
    });
  },
  inventory: function () {
    return api("/api/inventory").then(function (d) {
      stockAlert(d.stock);
      var cards = d.stock.map(function (i) {
        var low = i.quantity <= i.threshold, pct = Math.max(4, Math.min(100, Math.round(i.quantity / Math.max(i.threshold * 4, i.quantity, 1) * 100)));
        return '<div class="card inv-item"><div class="top"><div><div class="inv-name">' + esc(i.name) + '</div><div class="inv-meta">' + esc(i.pack) + '</div></div><span class="pill ' + (low ? "red" : "teal") + '">' + (low ? "Low stock" : "In stock") + '</span></div><div class="inv-stock">' + i.quantity + ' <small>' + esc(i.unit) + ' left</small></div><div class="inv-meta">' + egp(i.price) + ' each · Alert when ' + i.threshold + ' or fewer remain</div><div class="inv-bar"><i class="' + (low ? "low" : "") + '" style="width:' + pct + '%"></i></div></div>';
      }).join("");
      var opts = d.stock.map(function (i) { return '<option value="' + i.id + '">' + esc(i.name) + '</option>'; }).join("");
      var forms = can("inventory") ? '<div class="grid-2" style="margin-top:16px"><div class="card"><h2>Record a purchase</h2><p class="card-sub">Adds stock and goes straight into the clinic\'s expenses.</p><form id="buyForm" style="margin-top:12px"><div class="form-row"><label class="field">Item<select id="buyItem">' + opts + '</select></label><label class="field">Quantity<input id="buyQty" type="number" min="1" value="8" required /></label><label class="field">Price each (EGP)<input id="buyPrice" type="number" min="0" step="0.5" value="45" required /></label><button class="btn" type="submit">Add stock</button></div></form></div><div class="card"><h2>Record use with a patient</h2><p class="card-sub">The quantity is deducted from stock right away.</p><form id="useForm" style="margin-top:12px"><div class="form-row"><label class="field">Item<select id="useItem">' + opts + '</select></label><label class="field">Patient<input id="usePatient" required maxlength="120" placeholder="e.g. Omar Khalil" /></label><label class="field">Used<input id="useQty" type="number" min="1" value="1" required /></label><button class="btn" type="submit">Record use</button></div></form></div></div>' : "";
      var total = d.usage.reduce(function (s, u) { return s + u.cost; }, 0), qty = d.usage.reduce(function (s, u) { return s + u.qty; }, 0);
      return '<div class="inv-grid">' + cards + '</div>' + forms + '<div class="card" style="margin-top:16px"><h2>Usage report</h2><p class="card-sub">What was used, for whom, and what it cost. <strong>Latest entries: ' + qty + ' items used, costing ' + egp(total) + '.</strong></p><div class="table-scroll"><table><thead><tr><th>Item</th><th>Patient</th><th>When</th><th class="num">Qty</th><th class="num">Cost</th></tr></thead><tbody>' + d.usage.map(function (u) { return "<tr><td>" + esc(u.item) + "</td><td>" + esc(u.patient) + "</td><td>" + esc(u.when) + "</td><td class='num'>" + u.qty + "</td><td class='num'>" + egp(u.cost) + "</td></tr>"; }).join("") + '</tbody></table></div></div>';
    });
  },
  finances: function () {
    if (!can("finances")) return Promise.resolve(locked("Finances are private", "Your account cannot see the clinic's money. The doctor can open it from Team & settings."));
    return api("/api/finances").then(function (d) {
      var max = Math.max.apply(null, d.bars.map(function (b) { return b.amount; }).concat([1]));
      return '<div class="money-stats"><div class="stat"><div class="num">' + egp(d.collected) + '</div><div class="lbl">Collected from visits — ' + esc(d.month) + '</div></div><div class="stat"><div class="num">' + egp(d.spent) + '</div><div class="lbl">Total expenses — ' + esc(d.month) + '</div></div><div class="stat"><div class="num">' + egp(d.left) + '</div><div class="lbl">Left over</div></div></div><div class="grid-2"><div class="card"><h2>Where the money went</h2><div style="margin-top:8px">' + d.bars.map(function (b) { return '<div class="bar-row"><div>' + esc(b.name) + '</div><div class="bar-track"><i style="width:' + Math.round(b.amount / max * 100) + '%"></i></div><div class="bar-amt">' + egp(b.amount) + '</div></div>'; }).join("") + '</div></div><div class="card"><h2>How each person is paid</h2><p class="card-sub">Fixed salary or a percentage of their work — both are supported.</p><div>' + d.pay.map(function (p) { var fixed = p.kind === "fixed"; return '<div class="pay-row"><div class="pay-main"><div class="who">' + esc(p.name) + '</div><div class="how">' + esc(p.title) + ' · ' + (fixed ? "Fixed salary, paid monthly" : p.percent + "% of the " + egp(p.base) + " collected this month") + '</div></div><span class="pill ' + (fixed ? "grey" : "teal") + '">' + (fixed ? "Fixed" : "Percentage") + '</span><div class="pay-amt">' + egp(p.amount) + '</div></div>'; }).join("") + '</div></div></div><div class="card" style="margin-top:16px"><h2>Expenses</h2><div class="table-scroll"><table><thead><tr><th>Expense</th><th>Type</th><th>Date</th><th class="num">Amount</th></tr></thead><tbody>' + d.expenses.map(function (x) { return "<tr><td>" + esc(x.name) + "</td><td>" + esc(x.category) + "</td><td>" + esc(x.date) + "</td><td class='num'>" + egp(x.amount) + "</td></tr>"; }).join("") + '</tbody></table></div><form id="expForm" style="margin-top:14px"><div class="form-row"><label class="field">New expense<input id="expName" required maxlength="160" placeholder="e.g. Water bill" /></label><label class="field">Type<select id="expCat"><option>Rent</option><option>Electricity</option><option>Inventory</option><option>Salaries</option><option>Other</option></select></label><label class="field">Amount (EGP)<input id="expAmt" type="number" min="1" step="0.5" required placeholder="500" /></label><button class="btn" type="submit">Add expense</button></div></form></div>';
    });
  },
  rx: function () {
    if (!can("rx")) return Promise.resolve(locked("Prescriptions are written by doctors", "Your account cannot write prescriptions. The doctor can open this from Team & settings."));
    return api("/api/rx").then(function (d) {
      rxData = d;
      var meds = d.medicines.map(function (m) { var inRx = rx.some(function (r) { return r.id === m.id; }); return '<div class="med-card"><div class="mname">' + esc(m.name) + '</div><div class="mdose">' + esc(m.form) + ' · ' + esc(m.dose) + '</div><button class="btn small ' + (inRx ? "ghost" : "") + '" data-med="' + m.id + '">' + (inRx ? "Added ✓" : "Add") + '</button></div>'; }).join("");
      var stepper = function (kind, i, val, cap) { return '<div class="stepper-wrap"><div class="stepper"><button data-step="' + kind + '|-1|' + i + '" aria-label="Less">−</button><span class="val">' + val + '</span><button data-step="' + kind + '|1|' + i + '" aria-label="More">+</button></div><span class="cap">' + cap + '</span></div>'; };
      var list = rx.length ? rx.map(function (r, i) { return '<div class="rx-item"><div class="rx-main"><div class="rname">' + esc(r.name) + '</div><div class="rdose">' + esc(r.dose) + '</div></div>' + stepper("qty", i, r.qty, r.form.indexOf("bottle") > -1 ? "bottles" : "pieces") + stepper("days", i, r.days, "days") + '<button class="btn small danger" data-remove="' + i + '">Remove</button></div>'; }).join("") : '<p class="empty-note">No medicines yet. Tap “Add” on any medicine from the ready list.</p>';
      return '<div class="grid-2"><div class="stack"><div class="card"><h2>1 · Choose the patient</h2><div class="form-row" style="margin-top:10px"><label class="field">Patient<select id="rxPatient">' + d.patients.map(function (p) { return '<option value="' + p.id + '"' + (String(p.id) === window.rxPatientId ? " selected" : "") + '>' + esc(p.name) + '</option>'; }).join("") + '</select></label></div></div><div class="card"><h2>2 · Tap to add a medicine</h2><p class="card-sub">The clinic\'s ready list for ' + esc(me.clinic.specialty.toLowerCase()) + ' care.</p><div class="med-grid" style="margin-top:12px">' + meds + '</div></div></div><div class="stack"><div class="card"><h2>3 · Set amounts, then print</h2><div>' + list + '</div><div class="form-row" style="margin-top:14px"><button class="btn" id="rxDownload">Download prescription</button><button class="btn ghost" id="rxClear">Start over</button></div></div></div></div>';
    });
  },
  online: function () {
    if (!can("online")) return Promise.resolve(locked("Online consultations are for doctors", "Your account cannot run consultations. The doctor can open this from Team & settings."));
    return api("/api/consultations").then(function (d) {
      var meta = {"new":["New request","amber"],accepted:["Accepted","teal"],incall:["In call now","teal"],done:["Finished","grey"]};
      var acts = {"new":[["accept","Accept",""],["decline","Decline"," danger"]],accepted:[["start","Start video call",""]],incall:[["end","End call",""]]};
      return '<div class="grid-2"><div class="card"><h2>Requests</h2><div>' + (d.items.map(function (c) { return '<div class="consult"><div class="consult-main"><div class="who">' + esc(c.patient) + ' <span class="pill ' + meta[c.status][1] + '">' + meta[c.status][0] + '</span></div><div class="why">' + esc(c.reason) + '</div><div class="why">' + esc(c.when) + ' · Video</div></div><div class="appt-actions">' + (acts[c.status] || []).map(function (x) { return '<button class="btn small' + x[2] + '" data-consult="' + c.id + '" data-act="' + x[0] + '">' + x[1] + '</button>'; }).join("") + '</div></div>'; }).join("") || '<p class="empty-note">No requests right now.</p>') + '</div></div><div class="card"><h2>How it works</h2><ol class="steps-list" style="margin-top:10px"><li><strong>The patient asks</strong> for an online consultation and picks a time.</li><li><strong>The request appears here</strong>, so nobody misses it.</li><li><strong>Accept, then start the call</strong> at the agreed time.</li></ol></div></div>';
    });
  },
  team: function () {
    if (!can("team")) return Promise.resolve(locked("Settings belong to the doctor", "Only the clinic owner can change the team and permissions."));
    return api("/api/team").then(function (d) {
      var s = d.specialties.filter(function (x) { return x.name === d.specialty; })[0];
      var toggle = function (p, role) { var on = p[role]; return '<button class="toggle' + (on ? " on" : "") + '" data-perm="' + p.id + '" data-role="' + role + '" data-next="' + !on + '">' + (on ? "On" : "Off") + '</button>'; };
      return '<div class="card"><h2>Clinic specialty</h2><p class="card-sub">Each specialty brings its own services.</p><div class="spec-grid" style="margin-top:14px">' + d.specialties.map(function (x) { return '<button class="spec-btn' + (x.name === d.specialty ? " on" : "") + '" data-spec="' + esc(x.name) + '">' + esc(x.name) + '<small>' + esc(x.tag) + '</small></button>'; }).join("") + '</div><div style="margin-top:14px"><h3>' + esc(s.name) + ' setup</h3><p style="color:var(--muted);margin-top:4px">Services: ' + s.services.map(esc).join(" · ") + '</p><ul class="feat-list">' + s.features.map(function (f) { return "<li>" + esc(f) + "</li>"; }).join("") + '</ul></div></div><div class="card" style="margin-top:16px"><h2>The team</h2><div>' + d.people.map(function (p) { return '<div class="person"><div class="person-main"><div class="who">' + esc(p.name) + '</div><div class="role">' + esc(p.title) + ' — ' + esc(p.note) + '</div></div></div>'; }).join("") + '</div></div><div class="card" style="margin-top:16px"><h2>Who can do what</h2><p class="card-sub">Tap a switch to open or close a permission. It applies on the server at once.</p><div class="perm-row" style="border-bottom:1px solid var(--line);padding-bottom:6px"><div></div><div class="perm-col-head">Assistant</div><div class="perm-col-head">Associate</div></div><div>' + d.perms.map(function (p) { return '<div class="perm-row"><div class="perm-name">' + esc(p.name) + '</div>' + toggle(p, "assistant") + toggle(p, "associate") + '</div>'; }).join("") + '</div><p style="color:var(--muted);font-size:13.5px;margin-top:12px">The doctor (owner) always has full access.</p></div>';
    });
  }
};

function render() {
  $("pageTitle").textContent = PAGES[current][0];
  $("pageSub").textContent = PAGES[current][1];
  var page = current;
  return RENDER[page]().then(function (html) {
    if (page !== current) return;
    $("loadError").hidden = true;
    $("page").innerHTML = html;
  }).catch(function (e) {
    if (e.status === 401) return;
    if (e.status === 403) { return loadMe().then(render); }
    $("loadError").textContent = "Could not load this page: " + e.message;
    $("loadError").hidden = false;
  });
}
function go(p) {
  current = p;
  [].forEach.call(document.querySelectorAll("#mainNav .nav-btn"), function (b) { b.classList.toggle("active", b.getAttribute("data-go") === p); });
  window.scrollTo({top: 0}); render();
}
function piastres(v) { return Math.round(parseFloat(v) * 100); }

function loadMe() {
  return api("/api/me").then(function (d) {
    me = d;
    $("loginView").hidden = true; $("appView").hidden = false;
    $("clinicName").textContent = d.clinic.name;
    $("clinicSub").textContent = d.clinic.specialty + " clinic · " + d.clinic.city;
    $("todayDate").textContent = d.today;
    $("whoAmI").textContent = d.name;
  });
}
function showLogin() { $("appView").hidden = true; $("loginView").hidden = false; }

$("loginForm").addEventListener("submit", function (e) {
  e.preventDefault();
  $("loginError").textContent = "";
  fetch("/api/login", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")}, body: JSON.stringify({username: $("loginUser").value, password: $("loginPass").value})})
    .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error); }); })
    .then(function () { $("loginPass").value = ""; return loadMe(); }).then(function () { go("today"); })
    .catch(function (err) { $("loginError").textContent = err.message; });
});
$("logout").addEventListener("click", function () { api("/api/logout", {}).then(function () { rx = []; me = null; location.reload(); }); });
$("mainNav").addEventListener("click", function (e) { var b = e.target.closest("[data-go]"); if (b) go(b.getAttribute("data-go")); });

$("page").addEventListener("change", function (e) { if (e.target.id === "rxPatient") window.rxPatientId = e.target.value; });
$("page").addEventListener("submit", function (e) {
  e.preventDefault();
  var f = e.target, b = f.querySelector("button[type=submit]");
  if (f.id === "bookForm") act("/api/appointments", {name: $("bkName").value, service: $("bkService").value, time: $("bkTime").value, source: $("bkSource").value, fee: piastres($("bkFee").value)}, b);
  if (f.id === "buyForm") act("/api/stock/purchase", {item: +$("buyItem").value, quantity: +$("buyQty").value, price: piastres($("buyPrice").value)}, b);
  if (f.id === "useForm") act("/api/stock/use", {item: +$("useItem").value, patient: $("usePatient").value, quantity: +$("useQty").value}, b);
  if (f.id === "expForm") act("/api/expenses", {name: $("expName").value, category: $("expCat").value, amount: piastres($("expAmt").value)}, b);
});
$("page").addEventListener("click", function (e) {
  var b = e.target.closest("button"); if (!b) return;
  var d = b.dataset;
  if (d.appt) act("/api/appointments/" + d.appt + "/" + d.act, {}, b);
  if (d.consult) act("/api/consultations/" + d.consult + "/" + d.act, {}, b);
  if (d.spec) act("/api/team/specialty", {name: d.spec}, b).then(loadMe);
  if (d.perm) act("/api/team/permission", {role: d.role, perm: d.perm, allowed: d.next === "true"}, b).then(loadMe);
  if (d.med) {
    var id = +d.med, m = rxData.medicines.filter(function (x) { return x.id === id; })[0];
    if (!rx.some(function (r) { return r.id === id; })) rx.push({id: id, name: m.name, form: m.form, dose: m.dose, qty: 14, days: 5});
    render();
  }
  if (d.step) {
    var p = d.step.split("|"), r = rx[+p[2]];
    if (p[0] === "qty") r.qty = Math.max(1, Math.min(200, r.qty + +p[1]));
    if (p[0] === "days") r.days = Math.max(1, Math.min(90, r.days + +p[1]));
    render();
  }
  if (d.remove) { rx.splice(+d.remove, 1); render(); }
  if (b.id === "rxClear") { rx = []; render(); toast("Prescription cleared"); }
  if (b.id === "rxDownload") {
    if (!rx.length) { toast("Add at least one medicine first"); return; }
    if (!$("rxPatient").value) { toast("No patients booked today"); return; }
    b.disabled = true;
    api("/api/prescriptions", {patient: +$("rxPatient").value, items: rx.map(function (r) { return {medicine: r.id, quantity: r.qty, days: r.days}; })}).then(function (res) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([res.text], {type: "text/plain"}));
      a.download = "prescription-" + res.id + ".txt";
      document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 400);
      toast("Prescription saved and downloaded");
    }).catch(function (err) { toast(err.message); }).finally(function () { b.disabled = false; });
  }
});

loadMe().then(function () { go("today"); }).catch(function () { showLogin(); });
