// Team & settings: the clinic's discount types (Saged, 2026-11-13). A name, a percent of the bill or a fixed amount,
// and whether the front desk may apply it. The doctor suggests one in the visit and the desk applies it in one tap
// (pay.js). One request per save (POST discount-types, or PATCH discount-types/{id}; "Stop using it" is a PATCH with
// is_active false), with the dialog's request key until a definite answer. The list is read again after each save.
import { errorMessage, get, isOk, newKey, save } from "./api.js";
import { Submitter, el, openDialog, wireClose } from "./dialogs.js";
import { amountInput, asciiDigits, localDigits, moneyText, parsePiastres, percentText } from "./numbers.js";
import { renderPending, toast } from "./shell.js";
import { t } from "./strings.js";

const card = document.querySelector("[data-discount-card]");
const dialog = document.getElementById("discount-dialog");
if (card && dialog) {
  const list = card.querySelector("[data-discount-list]");
  const status = card.querySelector("[data-discount-status]");
  const form = dialog.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const heading = dialog.querySelector("[data-discount-heading]");
  const valueLabel = dialog.querySelector("[data-value-label]");
  const stop = dialog.querySelector("[data-discount-stop]");
  const submitter = new Submitter(dialog, { label: t("discount_types") });
  let types = [];
  let editing = null;
  wireClose(dialog);

  const valueText = (d) => (d.kind === "percent" ? percentText(d.value) : moneyText(d.value));

  // Active types first, in their order: each with "Edit", and "Move up" from the second on. Stopped ones follow,
  // muted, with "Use it again". Every change is one request; the list is read again after it.
  // Removed types stay out of sight unless asked for (Saged, 2026-11-19): bills that used them keep their name.
  let showRemoved = false;
  function render() {
    const active = types.filter((d) => d.is_active);
    const removed = types.length - active.length;
    const rows = types.filter((d) => d.is_active || showRemoved).map((d) => {
      const li = el("li");
      if (!d.is_active) li.classList.add("stopped");
      const what = el("span");
      const name = el("bdi", null, d.name);
      name.dir = "auto";
      const strong = el("strong");
      strong.append(name);
      const detail = d.is_active ? `${valueText(d)} · ${d.desk_can_apply ? t("discount_desk_yes") : t("discount_desk_no")}`
                                 : `${valueText(d)} · ${t("discount_stopped_label")}`;
      what.append(strong, el("br"), el("span", "muted", detail));
      const actions = el("span", "row-actions");
      const button = (label, onClick) => {
        const b = el("button", "btn ghost", label);
        b.type = "button";
        b.setAttribute("aria-label", `${label}: ${d.name}`);
        b.addEventListener("click", onClick);
        return b;
      };
      if (d.is_active) {
        const at = active.indexOf(d);
        if (at > 0) actions.append(button(t("discount_up"), () => moveUp(at)));
        actions.append(button(t("recipe_edit"), (event) => open(d, event.currentTarget)));
        actions.append(button(t("discount_remove"), () => remove(d)));
      } else {
        actions.append(button(t("discount_use_again"), () => useAgain(d)));
      }
      li.append(what, actions);
      return li;
    });
    if (removed) {
      const toggle = el("button", "link-btn", showRemoved ? t("discount_hide_removed") : t("discount_show_removed", { n: localDigits(removed) }));
      toggle.type = "button";
      toggle.addEventListener("click", () => { showRemoved = !showRemoved; render(); });
      const li = el("li");
      li.append(toggle);
      rows.push(li);
    }
    list.replaceChildren(...rows);
    status.textContent = active.length ? "" : t("discount_none");
  }

  function remove(d) {
    quick("PATCH", `/api/v1/discount-types/${d.id}`, { is_active: false }, d.name, t("removed", { name: d.name }));
  }

  // One quick request each (no dialog): its own key, and the list read again whatever the answer.
  async function quick(method, url, body, label, done) {
    if (list.dataset.busy) return;
    list.dataset.busy = "1";
    status.textContent = "";
    const result = await save(method, url, body, { key: newKey(), label: `${t("discount_types")} · ${label}` });
    delete list.dataset.busy;
    if (result.settled && isOk(result)) toast(done);
    else if (!result.settled) renderPending();
    await load();
    if (result.settled && !isOk(result)) status.textContent = errorMessage(result, t("failed"));
  }

  function moveUp(at) {
    const ids = types.filter((d) => d.is_active).map((d) => d.id);
    [ids[at - 1], ids[at]] = [ids[at], ids[at - 1]];
    const moved = types.find((d) => d.id === ids[at - 1]);
    quick("PUT", "/api/v1/discount-types/order", { ids }, moved.name, t("discount_moved", { name: moved.name }));
  }

  function useAgain(d) {
    quick("PATCH", `/api/v1/discount-types/${d.id}`, { is_active: true }, d.name, t("discount_type_saved", { name: d.name }));
  }

  function showKind() {
    valueLabel.textContent = field("kind").value === "percent" ? t("discount_percent_label") : t("discount_amount_label");
  }

  function open(d, opener) {
    if (!submitter.holdsUnknown()) {  // never a new key over an unconfirmed save
      editing = d;
      submitter.reset();
      form.reset();
      if (d) {
        field("name").value = d.name;
        field("kind").value = d.kind;
        field("value").value = d.kind === "percent" ? localDigits(d.value) : amountInput(d.value);
        field("desk_can_apply").checked = d.desk_can_apply;
      }
      heading.textContent = d ? t("discount_edit") : t("discount_add");
      stop.hidden = !d;
      showKind();
    }
    openDialog(dialog, opener);
  }

  // The typed values as the request body, or null. A percent is whole, 1 to 100; an amount is piastres above 0.
  function read() {
    const name = field("name").value.trim();
    const kind = field("kind").value;
    const typed = field("value").value.trim();
    const percent = /^\d{1,3}$/.test(asciiDigits(typed)) ? Number(asciiDigits(typed)) : 0;
    const value = kind === "percent" ? (percent >= 1 && percent <= 100 ? percent : null) : parsePiastres(typed) || null;
    if (!name || !value) return null;
    return { name, kind, value, desk_can_apply: field("desk_can_apply").checked };
  }

  async function load() {
    const result = await get("/api/v1/discount-types?all=1");
    if (!isOk(result)) { status.textContent = errorMessage(result, t("failed")); return; }
    types = result.data.items;
    render();
  }

  async function saveType(body) {
    const was = editing;
    const url = was ? `/api/v1/discount-types/${was.id}` : "/api/v1/discount-types";
    const name = (body && body.name) || (was && was.name) || "";
    const result = await submitter.run(was ? "PATCH" : "POST", url, body, `${t("discount_types")} · ${name}`);
    if (!result || !isOk(result)) return;
    const saved = result.data.discount_type;
    dialog.close();
    toast(t(saved.is_active ? "discount_type_saved" : "discount_type_stopped", { name: saved.name }));
    await load();
  }

  form.addEventListener("change", (event) => { if (event.target.name === "kind") showKind(); });
  stop.addEventListener("click", () => { if (!submitter.unknown && editing) saveType({ is_active: false }); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submitter.unknown) { saveType(null); return; }  // "Try again": the same key and body
    const body = read();
    if (!body) { submitter.say(t("discount_bad")); return; }
    saveType(body);
  });
  card.querySelector("[data-discount-add]").addEventListener("click", (event) => open(null, event.currentTarget));

  status.textContent = t("loading");
  load();
}
