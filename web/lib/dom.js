// The small vocabulary every screen is built from.
"use strict";

import {tg, haptic, bindMain, unbindMain, restoreMain, setPageLocked, resizeViewport} from "./tg.js";

export const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};

const modal = document.querySelector("#modal");
const modalBody = document.querySelector("#modal-body");
const noticeNode = document.querySelector("#notice");

// An open dialog lives in the top layer, so a toast left in the body renders
// behind it — exactly when it carries the error that explains what failed.
export function notify(message) {
  (modal.open ? modal : document.body).append(noticeNode);
  noticeNode.textContent = message;
  noticeNode.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { noticeNode.hidden = true; }, 6500);
}

export function button(text, action, cls = "") {
  const node = el("button", text, cls);
  node.type = "button";
  node.addEventListener("click", async () => {
    if (node.disabled) return;
    node.disabled = true;
    haptic("light");
    try { await action(); } catch (error) { haptic("error"); notify(error.message); } finally { node.disabled = false; }
  });
  return node;
}

export function row(name, value, cls = "") {
  const node = el("div", undefined, "row");
  node.append(el("span", name), el("span", value, `amount ${cls}`));
  return node;
}

export function field(parent, label, type = "text", value = "") {
  const wrap = el("label", label);
  const input = el("input");
  input.type = type;
  input.value = value;
  input.autocomplete = "off";
  wrap.append(input);
  parent.append(wrap);
  return input;
}

export function select(parent, label, options, value) {
  const wrap = el("label", label);
  const input = el("select");
  options.forEach(([id, name]) => { const option = el("option", name); option.value = id; input.append(option); });
  input.value = value;
  wrap.append(input);
  parent.append(wrap);
  return input;
}

// -- dialog ----------------------------------------------------------------
// Each opening gets its own container, so the listeners a form hangs on its
// box die with it instead of firing inside the next dialog.
export function openModal(title) {
  const box = el("div");
  const heading = el("h2", title);
  heading.id = "modal-title";
  box.append(heading);
  modalBody.replaceChildren(box);
  unbindMain();
  if (!modal.open) { setPageLocked(true); modal.showModal(); resizeViewport(); }
  tg?.BackButton.show();
  return box;
}
export const closeModal = () => modal.close();
export const modalOpen = () => modal.open;

/** The dialog's own primary action, mirrored into Telegram's main button. */
export function primary(box, text, action) {
  const node = button(text, action, "wide");
  box.append(node);
  if (bindMain(text, action)) node.hidden = true;
  return node;
}

export function ask(question, action) {
  const run = () => action().catch(error => { haptic("error"); notify(error.message); });
  if (tg?.isVersionAtLeast?.("6.2")) { tg.showConfirm(question, confirmed => { if (confirmed) run(); }); return; }
  const box = openModal("Подтвердите");
  box.append(el("p", question));
  primary(box, "Да", action);
}

export function onModalClose(handler) {
  modal.addEventListener("close", () => {
    if (modal.open) return; // A queued close event must not clear a newly opened dialog.
    setPageLocked(false);
    modalBody.replaceChildren();
    restoreMain();
    handler();
  });
}

// -- screen shell ----------------------------------------------------------
export function skeletons(count = 3) {
  const box = el("div");
  for (let index = 0; index < count; index++) box.append(el("div", undefined, "card skeleton"));
  return box;
}

export function failure(message, retry) {
  const box = el("div", undefined, "empty");
  box.append(el("p", message), button("Повторить", retry, ""));
  return box;
}

/** A card list: one landmark, one focus stop per row. */
export function cardList(items, render) {
  const list = el("ul", undefined, "cards");
  list.setAttribute("role", "list");
  items.forEach(item => { const line = el("li"); line.append(render(item)); list.append(line); });
  return list;
}
