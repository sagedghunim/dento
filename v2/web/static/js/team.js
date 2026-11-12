// Team & settings (design 5.8, G1, G2, G3). The "Who can do what" table is drawn from GET /api/v1/team: rows are
// the permission groups, columns the members. Every change is one request through a Submitter (one key until a
// definite answer), with the member's version; a 409 `stale` reads the team again and says so.
import { errorMessage, get, isOk } from "./api.js";
import { Submitter, el, openDialog, wireClose } from "./dialogs.js";
import { asciiDigits } from "./numbers.js";
import { alertMessage, toast } from "./shell.js";
import { t } from "./strings.js";

const FLASH = "hamada.team.flash";
const readJson = (id) => { const node = document.getElementById(id); return node ? JSON.parse(node.textContent) : null; };
const team = readJson("team-data");
const isStale = (result) => result && result.status === 409 && result.data && result.data.error && result.data.error.code === "stale";

// A message kept across a reload (the settings card reloads the page after a conflict).
try { const text = sessionStorage.getItem(FLASH); if (text) { sessionStorage.removeItem(FLASH); toast(text); } } catch (e) { /* none */ }
function reloadWith(text) {
  try { sessionStorage.setItem(FLASH, text); } catch (e) { /* none */ }
  window.location.reload();
}

const roleName = (role) => t(`team_role_${role}`);
const bdi = (text, className) => { const node = el("bdi", className, text); node.dir = "auto"; return node; };

if (team) initTeam();
initSettings();

// --- the team ---------------------------------------------------------------------------------------------------

function initTeam() {
  const card = document.querySelector("[data-matrix-card]");
  const holder = card.querySelector("[data-matrix]");
  const pick = card.querySelector("[data-pick]");
  const columnsSelect = card.querySelector("[data-columns]");
  const tryButton = card.querySelector("[data-try]");
  const matrixForm = card.querySelector("[data-matrix-form]");
  const submitter = new Submitter(card);
  const membersList = document.querySelector("[data-members]");
  const overviewList = document.querySelector("[data-overview]");
  const personBar = card.querySelector("[data-person-bar]");
  const placesBarButton = card.querySelector("[data-bar-places]");
  const pickKey = `hamada.team.columns.${team.me}`;
  const open = new Set();  // the group rows showing their single permissions
  let pending = null;      // {memberId, what} of the request in flight or unconfirmed

  const me = () => team.members.find((m) => m.id === team.me) || { codes: [] };
  const byId = (id) => team.members.find((m) => m.id === id);
  const ownerOnly = new Set(team.groups.flatMap((g) => g.codes.filter((c) => c.owner_only).map((c) => c.code)));
  const codeLabel = new Map(team.groups.flatMap((g) => g.codes.map((c) => [c.code, c.label])));
  const groupLabel = new Map(team.groups.map((g) => [g.key, g.label]));

  // Columns: active members other than the owner (the owner always has everything).
  const people = () => team.members.filter((m) => m.is_active && m.role !== "owner");

  // The table shows everyone (up to 4), all doctors, all assistants, or one person.
  function chosen() {
    const all = people();
    pick.hidden = all.length < 2;
    const options = [...(all.length <= 4 ? [["all", t("team_everyone")]] : []),
      ["doctor", t("team_all_doctors")], ["assistant", t("team_all_assistants")],
      ...all.map((m) => [String(m.id), `${m.name} · ${roleName(m.role)}`])];
    let value = columnsSelect.value;
    if (!value) { try { value = localStorage.getItem(pickKey) || ""; } catch (e) { value = ""; } }
    if (!options.some(([v]) => v === value)) value = options[0][0];
    columnsSelect.replaceChildren(...options.map(([v, label]) => { const o = el("option", null, label); o.value = v; return o; }));
    columnsSelect.value = value;
    const one = all.find((m) => String(m.id) === value);
    personBar.hidden = !one;
    if (one) placesBarButton.hidden = !(team.places && team.is_owner && one.editable);
    if (value === "all") return all;
    if (value === "doctor" || value === "assistant") return all.filter((m) => m.role === value);
    return one ? [one] : [];
  }
  columnsSelect.addEventListener("change", () => {
    try { localStorage.setItem(pickKey, columnsSelect.value); } catch (e) { /* none */ }
    draw();
  });

  // Why a switch can't be used, or "" when it can. The server checks every change again.
  function blocked(member, codes) {
    if (member.is_me) return t("team_not_self");
    if (!member.editable) return t("team_not_editable");
    if (team.is_owner) return "";
    if (codes.some((c) => ownerOnly.has(c))) return t("team_owner_only");
    const mine = new Set(me().codes);
    if (codes.some((c) => !mine.has(c))) return t("team_not_held");
    return "";
  }

  function switchButton(state, label, reason, data) {
    const button = el("button", `toggle ${state}`, t(`team_${state}`));
    button.type = "button";
    button.setAttribute("aria-pressed", state === "on" ? "true" : state === "some" ? "mixed" : "false");
    button.setAttribute("aria-label", `${label}: ${t(`team_${state}`)}`);
    Object.assign(button.dataset, data);
    if (reason) { button.disabled = true; button.title = reason; }
    return button;
  }

  function draw() {
    const columns = chosen();
    if (!columns.length) {
      holder.replaceChildren(el("p", "empty-note", t("team_no_members")));
      return;
    }
    const table = el("table", "matrix");
    const head = el("tr");
    const corner = el("th", "matrix-corner");
    corner.append(el("span", "visually-hidden", t("team_permission")));
    head.append(corner);
    for (const m of columns) {
      const th = el("th", "matrix-col");
      th.scope = "col";
      th.append(bdi(m.name, "matrix-name"), el("small", null, m.title || roleName(m.role)));
      head.append(th);
    }
    const thead = el("thead");
    thead.append(head);
    const tbody = el("tbody");
    for (const group of team.groups) {
      const row = el("tr", "matrix-group");
      const th = el("th");
      th.scope = "row";
      const label = el("button", "matrix-label");
      label.type = "button";
      label.dataset.expand = group.key;
      label.setAttribute("aria-expanded", String(open.has(group.key)));
      label.append(el("span", "matrix-title", group.label), el("small", null, group.description));
      th.append(label);
      row.append(th);
      const codes = group.codes.map((c) => c.code);
      for (const m of columns) {
        const state = m.groups[group.key] === "partial" ? "some" : m.groups[group.key];
        const td = el("td");
        td.append(switchButton(state, `${group.label}, ${m.name}`, blocked(m, codes), { member: m.id, group: group.key }));
        row.append(td);
      }
      tbody.append(row);
      if (!open.has(group.key)) continue;
      for (const code of group.codes) {
        const sub = el("tr", "matrix-code");
        const name = el("th");
        name.scope = "row";
        name.append(el("span", null, code.label));
        if (code.owner_only) name.append(el("small", null, t("team_owner_code")));
        sub.append(name);
        for (const m of columns) {
          const td = el("td");
          const state = m.codes.includes(code.code) ? "on" : "off";
          td.append(switchButton(state, `${code.label}, ${m.name}`, blocked(m, [code.code]), { member: m.id, code: code.code }));
          sub.append(td);
        }
        tbody.append(sub);
      }
    }
    table.append(thead, tbody);
    if (pending) table.setAttribute("aria-busy", "true");
    holder.replaceChildren(table);
  }

  async function reload(message) {
    const result = await get("/api/v1/team");
    if (isOk(result)) {
      team.groups = result.data.groups;
      team.members = result.data.members;
      team.members.forEach(followHere);
      draw();
      drawMembers();
      drawOverview();
    }
    if (message) toast(message);
  }

  // One switch: a whole group, or one permission inside an open group.
  async function flip(button) {
    if (card.dataset.busy || submitter.holdsUnknown()) return;  // one change at a time; a double tap does nothing
    const member = byId(Number(button.dataset.member));
    if (!member) return;
    const body = { version: member.version };
    let what;
    if (button.dataset.group) {
      const key = button.dataset.group;
      body.groups = { [key]: member.groups[key] !== "on" };
      what = { name: groupLabel.get(key), on: body.groups[key] };
    } else {
      const code = button.dataset.code;
      body.codes = { [code]: !member.codes.includes(code) };
      what = { name: codeLabel.get(code), on: body.codes[code] };
    }
    pending = { member: member.name, ...what };
    button.classList.add("saving");
    await settle(await submitter.run("PUT", `/api/v1/team/members/${member.id}/permissions`, body,
      t("team_perm_saved", { what: what.name, name: member.name, state: t(what.on ? "team_on" : "team_off") })));
  }

  async function settle(result) {
    tryButton.hidden = Boolean(result);
    if (!result) { draw(); return; }  // unknown: "Try again" or "Discard" (the same key, never twice)
    const done = pending;
    pending = null;
    if (isOk(result)) {
      const fresh = result.data.member;
      if (fresh) team.members = team.members.map((m) => (m.id === fresh.id ? fresh : m));
      if (fresh) followHere(fresh);
      draw();
      drawMembers();
      drawOverview();
      if (done) toast(t("team_perm_saved", { what: done.name, name: done.member, state: t(done.on ? "team_on" : "team_off") }));
      return;
    }
    if (isStale(result)) { submitter.say(""); await reload(errorMessage(result, t("failed"))); return; }
    draw();
  }

  holder.addEventListener("click", (event) => {
    const expand = event.target.closest("[data-expand]");
    if (expand) {
      const key = expand.dataset.expand;
      if (open.has(key)) open.delete(key); else open.add(key);
      draw();
      const again = holder.querySelector(`[data-expand="${key}"]`);
      if (again) again.focus();
      return;
    }
    const button = event.target.closest("button.toggle");
    if (button && !button.disabled) flip(button);
  });
  matrixForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!submitter.holdsUnknown() || card.dataset.busy) return;
    await settle(await submitter.run());
  });
  card.querySelector("[data-discard]").addEventListener("click", () => { pending = null; tryButton.hidden = true; draw(); });

  // --- who works where: each person, their places with role and groups there, and their sections ---

  function followHere(member) {
    const person = (team.overview.people || []).find((p) => p.member_id === member.id);
    const here = person && person.places.find((p) => p.current);
    if (here) { here.role = member.role; here.groups = member.groups; }
  }

  function groupChip(group, state) {
    const chip = el("span", `gchip ${state}`);
    chip.append(group.label);
    // The state is in words too, never in the colour alone.
    if (state === "partial") chip.append(el("small", null, ` · ${t("team_some")}`));
    if (state === "off") chip.append(el("span", "visually-hidden", ` · ${t("team_off")}`));
    return chip;
  }

  function drawOverview() {
    const list = (team.overview.people || []).map((person) => {
      const m = byId(person.member_id);
      if (!m || !m.is_active) return null;
      const li = el("li", "person");
      const head = el("button", "person-head");
      head.type = "button";
      head.dataset.person = m.id;
      head.append(bdi(m.name, "person-name"), el("span", "pill grey", m.title || roleName(m.role)));
      li.append(head);
      if (person.sections.length) li.append(el("p", "muted", t("team_sections_only", { list: person.sections.join("، ") })));
      for (const place of person.places) {
        const row = el("div", "person-place");
        const name = el("p", "place-name");
        name.append(bdi(place.name));
        if (place.unavailable) {
          name.append(" · ", el("span", "muted", t("team_place_unavailable")));
          row.append(name);
          li.append(row);
          continue;
        }
        name.append(" · ", el("span", "muted", roleName(place.role)));
        const chips = el("div", "gchips");
        for (const group of team.groups) chips.append(groupChip(group, place.groups[group.key] || "off"));
        row.append(name, chips);
        li.append(row);
      }
      return li;
    }).filter(Boolean);
    overviewList.replaceChildren(...(list.length ? list : [el("p", "empty-note", t("team_no_members"))]));
  }

  // Tapping a person shows only their switches (this place), with their details and places beside them.
  overviewList.addEventListener("click", (event) => {
    const head = event.target.closest("[data-person]");
    if (!head) return;
    columnsSelect.value = head.dataset.person;
    try { localStorage.setItem(pickKey, columnsSelect.value); } catch (e) { /* none */ }
    draw();
    card.scrollIntoView({ block: "start", behavior: "smooth" });
    const first = holder.querySelector("button.toggle:not(:disabled)") || columnsSelect;
    first.focus({ preventScroll: true });
  });
  const pickedPerson = () => byId(Number(columnsSelect.value));
  card.querySelector("[data-bar-edit]").addEventListener("click", (event) => openMember(pickedPerson(), event.currentTarget));
  placesBarButton.addEventListener("click", (event) => openPlaces(pickedPerson(), event.currentTarget));

  // --- the members list ---

  const sectionName = new Map((team.sections || []).map((s) => [s.id, s.name]));

  function drawMembers() {
    membersList.replaceChildren(...team.members.map((m) => {
      const li = el("li");
      li.dataset.memberRow = m.id;
      const main = el("div", "med-main");
      const top = el("strong");
      top.append(bdi(m.name));
      const tags = [roleName(m.role)];
      if (m.is_me) tags.push(t("team_you"));
      main.append(top, " ");
      for (const tag of tags) main.append(el("span", "pill grey", tag), " ");
      if (!m.is_active) main.append(el("span", "pill red", t("team_inactive")));
      const line = [];
      if (m.title) line.push(m.title);
      if (m.role === "assistant" && sectionName.size) {
        line.push(m.sections.length ? m.sections.map((id) => sectionName.get(id) || "").filter(Boolean).join("، ") : t("team_every_section"));
      }
      if (line.length) { const p = el("p", "muted"); p.append(bdi(line.join(" · "))); main.append(p); }
      li.append(main);
      if (m.editable || m.is_me) {
        const actions = el("div", "row-actions");
        const edit = el("button", "btn ghost", t("edit"));
        edit.type = "button";
        edit.dataset.editMember = m.id;
        actions.append(edit);
        li.append(actions);
      }
      return li;
    }));
  }
  membersList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-edit-member]");
    if (button) openMember(byId(Number(button.dataset.editMember)), button);
  });

  // Section checkboxes for an assistant in a center: none ticked means every section.
  function sectionChecks(box, ticked) {
    const holder = box.querySelector("[data-section-checks]");
    holder.replaceChildren(...(team.sections || []).map((s) => {
      const label = el("label", "chip");
      const input = el("input");
      input.type = "checkbox";
      input.name = "sections";
      input.value = s.id;
      input.checked = ticked.includes(s.id);
      const span = el("span");
      span.append(bdi(s.name));
      label.append(input, span);
      return label;
    }));
  }
  const tickedSections = (form) => [...form.querySelectorAll('[name="sections"]:checked')].map((i) => Number(i.value));
  const roleOf = (form) => { const c = form.querySelector('[name="role"]:checked'); return c ? c.value : ""; };

  function showPassword(box, account) {
    box.replaceChildren();
    if (!account || !account.temporary_password) { box.hidden = true; return; }
    const rows = [[t("team_username"), account.username], [t("team_temp_password"), account.temporary_password]];
    const list = el("dl", "password-list");
    for (const [label, value] of rows) {
      const dd = el("dd");
      dd.append(el("bdi", "ltr num", value));
      list.append(el("dt", null, label), dd);
    }
    box.append(list, el("p", "muted", t("team_password_note")));
    box.hidden = false;
  }

  // --- one member: names, role, sections, status, a temporary password, places ---

  const memberDialog = document.getElementById("member-dialog");
  const memberForm = memberDialog.querySelector("form");
  const memberSub = new Submitter(memberDialog);
  const memberField = (name) => memberForm.elements.namedItem(name);
  const passwordBox = memberDialog.querySelector("[data-password-box]");
  let editing = null;
  wireClose(memberDialog);

  function openMember(member, opener) {
    if (memberSub.holdsUnknown()) { openDialog(memberDialog, opener); return; }
    if (!member) return;
    editing = member;
    memberForm.reset();
    memberSub.reset();
    showPassword(passwordBox, null);
    memberDialog.querySelector("[data-member-name]").textContent = member.name;
    memberField("display_name").value = member.display_name || "";
    memberField("display_name_en").value = member.display_name_en || "";
    memberField("title").value = member.title || "";
    const full = member.editable && !member.is_me;  // names only for yourself (design G1)
    const roleBox = memberDialog.querySelector("[data-role-box]");
    roleBox.hidden = !full || member.role === "owner";
    memberForm.querySelectorAll('[name="role"]').forEach((input) => { input.checked = input.value === member.role; });
    memberDialog.querySelector("[data-active-box]").hidden = !full;
    memberField("is_active").checked = member.is_active;
    sectionChecks(memberDialog.querySelector("[data-sections-box]"), member.sections || []);
    toggleSections(memberDialog, memberForm, full);
    memberDialog.querySelector("[data-extra-actions]").hidden = !full;
    memberDialog.querySelector("[data-open-places]").hidden = !(team.places && team.is_owner && member.is_active);
    openDialog(memberDialog, opener);
  }

  function toggleSections(dialog, form, allowed) {
    const box = dialog.querySelector("[data-sections-box]");
    box.hidden = !(allowed && (team.sections || []).length && roleOf(form) === "assistant");
  }
  memberForm.addEventListener("change", (event) => {
    if (event.target.name === "role") toggleSections(memberDialog, memberForm, editing && editing.editable && !editing.is_me);
  });

  function memberChanges() {
    const changes = {};
    for (const name of ["display_name", "display_name_en", "title"]) {
      const value = memberField(name).value.trim();
      if (value !== (editing[name] || "")) changes[name] = value;
    }
    if (editing.editable && !editing.is_me) {
      const role = roleOf(memberForm);
      if (role && role !== editing.role && editing.role !== "owner") changes.role = role;
      if (memberField("is_active").checked !== editing.is_active) changes.is_active = memberField("is_active").checked;
      const box = memberDialog.querySelector("[data-sections-box]");
      if (!box.hidden) {
        const sections = tickedSections(memberForm).sort((a, b) => a - b);
        if (sections.join(",") !== [...(editing.sections || [])].sort((a, b) => a - b).join(",")) changes.sections = sections;
      }
    }
    return changes;
  }

  memberForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (memberDialog.dataset.busy) return;
    let request = memberSub.unknown;
    if (!request) {
      if (!memberField("display_name").value.trim()) { memberSub.say(t("team_need_name")); return; }
      const changes = memberChanges();
      if (!Object.keys(changes).length) { memberDialog.close(); return; }
      request = { method: "PATCH", url: `/api/v1/team/members/${editing.id}`, body: { version: editing.version, ...changes } };
    }
    const result = await memberSub.run(request.method, request.url, request.body, t("team_saved", { name: editing.name }));
    if (!result) return;
    if (request.url.endsWith("/temporary-password")) { afterPassword(result); return; }
    if (isOk(result)) {
      memberDialog.close();
      toast(t("team_saved", { name: result.data.member.name }));
      await reload();
      return;
    }
    if (isStale(result)) { memberDialog.close(); await reload(errorMessage(result, t("failed"))); }
  });

  memberDialog.querySelector("[data-temp-password]").addEventListener("click", async () => {
    if (memberDialog.dataset.busy || memberSub.holdsUnknown()) return;
    showPassword(passwordBox, null);
    afterPassword(await memberSub.run("POST", `/api/v1/team/members/${editing.id}/temporary-password`, {},
      t("team_password_for", { name: editing.name })));
  });

  function afterPassword(result) {
    if (!result || !isOk(result)) return;
    if (result.data.account) showPassword(passwordBox, result.data.account);
    else memberSub.say(t("team_password_replayed"));  // a repeat shows no password (B6)
  }

  // --- adding a member: a new account (its password shown once) or an invitation ---

  const addDialog = document.getElementById("add-dialog");
  const addForm = addDialog.querySelector("form");
  const addSub = new Submitter(addDialog);
  const addField = (name) => addForm.elements.namedItem(name);
  const addFields = addDialog.querySelector("[data-add-fields]");
  const addBox = addDialog.querySelector("[data-password-box]");
  const addButton = addDialog.querySelector("[data-submit]");
  wireClose(addDialog);

  const modeOf = () => addForm.querySelector('[name="mode"]:checked').value;
  function addMode() {
    addDialog.querySelectorAll("[data-new-only]").forEach((node) => { node.hidden = modeOf() !== "new"; });
    toggleSections(addDialog, addForm, true);
    addButton.textContent = modeOf() === "new" ? t("team_add") : t("team_invite");
  }
  addForm.addEventListener("change", (event) => { if (event.target.name === "mode" || event.target.name === "role") addMode(); });

  document.querySelector("[data-add-member]").addEventListener("click", (event) => {
    if (addSub.holdsUnknown()) { openDialog(addDialog, event.currentTarget); return; }
    addForm.reset();
    addSub.reset();
    addFields.hidden = false;
    addButton.hidden = false;
    showPassword(addBox, null);
    sectionChecks(addDialog.querySelector("[data-sections-box]"), []);
    addMode();
    openDialog(addDialog, event.currentTarget);
  });

  function addBody() {
    const mode = modeOf();
    const value = (name) => addField(name).value.trim();
    const body = { mode, username: value("username").toLowerCase(), role: roleOf(addForm),
                   display_name: value("display_name") || (mode === "new" ? value("full_name") : ""),
                   display_name_en: value("display_name_en"), title: value("title"), language: document.documentElement.lang };
    if (mode === "new") { body.full_name = value("full_name"); body.phone = asciiDigits(value("phone")); }
    if (!addDialog.querySelector("[data-sections-box]").hidden) body.sections = tickedSections(addForm);
    return body;
  }

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (addDialog.dataset.busy) return;
    const body = addSub.unknown ? addSub.unknown.body : addBody();
    if (!addSub.unknown) {
      if (body.mode === "new" && body.full_name.length < 2) { addSub.say(t("need_name")); return; }
      if (!body.username) { addSub.say(t("team_need_username")); return; }
      if (!body.display_name) { addSub.say(t("team_need_name")); return; }
    }
    const result = await addSub.run("POST", "/api/v1/team/members", body, t("team_adding", { name: body.display_name }));
    if (!result || !isOk(result)) return;
    if (result.status === 202) {
      addDialog.close();
      toast(t("team_invited", { username: body.username }));
      return;
    }
    await reload();
    if (result.data.account && result.data.account.temporary_password) {
      addFields.hidden = true;
      addButton.hidden = true;  // the dialog now only shows the password, once
      showPassword(addBox, result.data.account);
      toast(t("team_saved", { name: result.data.member.name }));
    } else {
      addDialog.close();
      alertMessage(t("team_password_replayed"));
    }
  });

  // --- places (the owner only): the same person in the owner's other places ---

  const placesDialog = document.getElementById("places-dialog");
  const placesForm = placesDialog.querySelector("form");
  const placesSub = new Submitter(placesDialog);
  const placeChecks = placesDialog.querySelector("[data-place-checks]");
  let placesOf = null;
  wireClose(placesDialog);

  memberDialog.querySelector("[data-open-places]").addEventListener("click", () => {
    if (memberDialog.dataset.busy) return;
    const member = editing;
    if (!memberSub.holdsUnknown()) memberDialog.close();
    openPlaces(member, document.querySelector(`[data-edit-member="${member.id}"]`));
  });

  async function openPlaces(member, opener) {
    if (!member) return;
    if (placesSub.holdsUnknown()) { openDialog(placesDialog, opener); return; }
    placesOf = member;
    placesForm.reset();
    placesSub.reset();
    placesDialog.querySelector("[data-member-name]").textContent = member.name;
    placeChecks.replaceChildren(el("p", "muted", t("loading")));
    openDialog(placesDialog, opener);
    const result = await get(`/api/v1/team/members/${member.id}/places`);
    if (placesOf !== member) return;
    if (!isOk(result)) { placeChecks.replaceChildren(el("p", "form-error", errorMessage(result, t("failed")))); return; }
    placeChecks.replaceChildren(...result.data.items.map((place) => {
      const label = el("label", "check");
      const input = el("input");
      input.type = "checkbox";
      input.name = "place";
      input.value = place.clinic_id;
      input.checked = place.current || Boolean(place.member && place.member.is_active);
      input.disabled = place.current;  // this place changes through the member's own dialog
      label.append(input, " ", bdi(place.name));
      if (place.current) label.append(" ", el("small", "muted", t("team_this_place")));
      return label;
    }));
  }

  placesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (placesDialog.dataset.busy || !placesOf) return;
    const body = placesSub.unknown ? placesSub.unknown.body : {
      places: [...placesForm.querySelectorAll('[name="place"]:checked:not(:disabled)')].map((i) => ({ clinic_id: Number(i.value) })),
      copy: placesForm.elements.namedItem("copy").checked,
    };
    const result = await placesSub.run("PUT", `/api/v1/team/members/${placesOf.id}/places`, body,
      t("team_places_saved", { name: placesOf.name }));
    if (!result || !isOk(result)) return;
    placesDialog.close();
    reloadWith(t("team_places_saved", { name: placesOf.name }));  // the overview reads every place again
  });

  draw();
  drawMembers();
  drawOverview();
}

// --- settings: the clinic's names and its sections' names (settings.manage) ------------------------------------

function initSettings() {
  const card = document.querySelector("[data-profile-card]");
  if (!card) return;
  const profile = readJson("profile-data") || {};
  const form = card.querySelector("[data-profile-form]");
  const submitter = new Submitter(card);
  const NAMES = ["name", "name_en", "city", "phone"];
  for (const name of NAMES) form.elements.namedItem(name).value = profile[name] || "";

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (card.dataset.busy) return;
    let body = submitter.unknown && submitter.unknown.body;
    if (!body) {
      const changes = {};
      for (const name of NAMES) {
        let value = form.elements.namedItem(name).value.trim();
        if (name === "phone") value = asciiDigits(value);
        if (value !== (profile[name] || "")) changes[name] = value;
      }
      if (!Object.keys(changes).length) { submitter.say(t("team_nothing_changed")); return; }
      if ("name" in changes && !changes.name) { submitter.say(t("team_need_name")); return; }
      body = { changes, expected: Object.fromEntries(Object.keys(changes).map((k) => [k, profile[k] || ""])) };
    }
    const result = await submitter.run("PUT", "/api/v1/settings/profile", body, t("settings_saved"));
    if (!result) return;
    if (isOk(result)) { Object.assign(profile, result.data.profile); toast(t("settings_saved")); return; }
    if (isStale(result)) reloadWith(errorMessage(result, t("failed")));
  });

  const dialog = document.getElementById("section-dialog");
  if (!dialog) return;
  const sections = readJson("sections-data") || [];
  const sectionForm = dialog.querySelector("form");
  const sectionSub = new Submitter(dialog);
  let section = null;
  wireClose(dialog);

  card.addEventListener("click", (event) => {
    const button = event.target.closest("[data-edit-section]");
    if (!button) return;
    if (sectionSub.holdsUnknown()) { openDialog(dialog, button); return; }
    section = sections.find((s) => s.id === Number(button.dataset.editSection));
    if (!section) return;
    sectionForm.reset();
    sectionSub.reset();
    sectionForm.elements.namedItem("name").value = section.name_ar || "";
    sectionForm.elements.namedItem("name_en").value = section.name_en || "";
    openDialog(dialog, button);
  });

  sectionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy || !section) return;
    let body = sectionSub.unknown && sectionSub.unknown.body;
    if (!body) {
      const now = { name: sectionForm.elements.namedItem("name").value.trim(), name_en: sectionForm.elements.namedItem("name_en").value.trim() };
      if (!now.name) { sectionSub.say(t("team_need_name")); return; }
      const before = { name: section.name_ar || "", name_en: section.name_en || "" };
      const changes = Object.fromEntries(Object.entries(now).filter(([k, v]) => v !== before[k]));
      if (!Object.keys(changes).length) { dialog.close(); return; }
      body = { changes, expected: Object.fromEntries(Object.keys(changes).map((k) => [k, before[k]])) };
    }
    const result = await sectionSub.run("PATCH", `/api/v1/sections/${section.id}`, body, t("settings_saved"));
    if (!result) return;
    if (isOk(result)) {
      const fresh = result.data.section;
      Object.assign(section, { name: fresh.name, name_ar: fresh.name_ar, name_en: fresh.name_en });
      const row = card.querySelector(`[data-section-row="${section.id}"]`);
      row.querySelector("[data-section-name]").textContent = fresh.name;
      row.querySelector("[data-section-pair]").textContent = fresh.name_en ? `${fresh.name_ar} · ${fresh.name_en}` : fresh.name_ar;
      dialog.close();
      toast(t("settings_saved"));
      return;
    }
    if (isStale(result)) { dialog.close(); reloadWith(errorMessage(result, t("failed"))); }
  });
}
