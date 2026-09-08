"use strict";
const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const modal = document.querySelector("#modal");
const modalBody = document.querySelector("#modal-body");
let me, currencies = [], currentGroup = null, currentTab = "groups", generation = 0;
const money = (cents, currency) => `${(cents / 100).toLocaleString("ru-RU", {minimumFractionDigits: 2, maximumFractionDigits: 2})} ${currency}`;
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
function notify(message) { const node = document.querySelector("#notice"); node.textContent = message; node.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => { node.hidden = true; }, 6500); }
function button(text, action, cls = "") {
  const node = el("button", text, cls); node.type = "button";
  node.addEventListener("click", async () => { if (node.disabled) return; node.disabled = true; try { await action(); } catch (error) { notify(error.message); } finally { node.disabled = false; } });
  return node;
}
async function api(path, method = "GET", body) {
  const response = await fetch(`/api${path}`, {method, headers: {"Authorization": `tma ${tg?.initData || ""}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})}, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store"});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Сервер недоступен. Попробуйте ещё раз.");
  return data;
}
function openModal(title) { modalBody.replaceChildren(el("h2", title)); if (!modal.open) modal.showModal(); return modalBody; }
function field(parent, label, type = "text", value = "") { const wrap = el("label", label); const input = el("input"); input.type = type; input.value = value; input.autocomplete = "off"; wrap.append(input); parent.append(wrap); return input; }
function select(parent, label, options, value) { const wrap = el("label", label); const input = el("select"); options.forEach(([id, name]) => { const option = el("option", name); option.value = id; input.append(option); }); input.value = value; wrap.append(input); parent.append(wrap); return input; }
function amount(value, allowZero = false) { const text = value.trim().replace(",", "."); if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error("Введите сумму с точностью до копеек."); const [whole, fraction = ""] = text.split("."); const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0")); if (!Number.isSafeInteger(cents) || cents > 1000000000 || cents < (allowZero ? 0 : 1)) throw new Error("Проверьте сумму."); return cents; }
function row(name, value, cls = "") { const node = el("div", undefined, "row"); node.append(el("span", name), el("span", value, `amount ${cls}`)); return node; }
function setTab(tab) { currentTab = tab; document.querySelector("#groups-tab").classList.toggle("active", tab === "groups"); document.querySelector("#debts-tab").classList.toggle("active", tab === "debts"); }
function backState(visible) { if (visible) tg?.BackButton.show(); else tg?.BackButton.hide(); }
async function showGroups() {
  const version = ++generation; currentGroup = null; setTab("groups"); backState(false);
  const data = await api("/groups"); if (version !== generation) return;
  app.replaceChildren(el("h2", "Мои группы"), el("p", "Путешествия, дом и всё, что делим вместе.", "intro"));
  const actions = el("div", undefined, "actions"); actions.append(button("+ Создать группу", createGroup), button("Вступить по коду", joinGroup, "secondary")); app.append(actions);
  if (!data.groups.length) app.append(el("div", "Пока нет групп. Создайте первую или введите код приглашения от друзей.", "empty"));
  data.groups.forEach(group => { const card = button("", () => showGroup(group.id), "card group"); const content = el("div"); content.append(el("h3", group.title), el("small", `${group.currency} · ${group.balance === 0 ? "Вы в расчёте" : group.balance > 0 ? `Вам должны ${money(group.balance, group.currency)}` : `Ваш долг ${money(-group.balance, group.currency)}`}`)); card.append(el("span", "◎", "icon"), content, el("span", "›", "arrow")); app.append(card); });
}
function createGroup() { const box = openModal("Новая группа"); const title = field(box, "Название"); title.maxLength = 100; const currency = select(box, "Валюта группы", currencies.map(c => [c, c]), "RUB"); box.append(button("Создать", async () => { if (!title.value.trim()) throw new Error("Введите название группы."); const group = await api("/groups", "POST", {title: title.value, currency: currency.value}); modal.close(); await showGroup(group.id); }, "wide")); }
function joinGroup() { const box = openModal("Вступить в группу"); const code = field(box, "Код приглашения"); code.maxLength = 64; box.append(button("Вступить", async () => { const group = await api("/join", "POST", {code: code.value.trim().replace(/^\/join\s+/, "")}); modal.close(); await showGroup(group.id); }, "wide")); }
async function showGroup(id) {
  const version = ++generation; currentGroup = id; setTab("groups"); backState(true);
  const group = await api(`/groups/${id}`); if (version !== generation) return;
  app.replaceChildren(button("‹ Все группы", showGroups, "quiet"), el("h2", group.title));
  const balance = group.balances[me.id] || 0; const hero = el("div", undefined, "hero"); hero.append(el("small", balance < 0 ? "Ваш долг в группе" : balance > 0 ? "Вам должны в группе" : "Все расходы учтены"), el("strong", balance === 0 ? "Вы в расчёте" : money(Math.abs(balance), group.currency))); app.append(hero);
  const actions = el("div", undefined, "actions"); actions.append(button("+ Добавить трату", () => expenseForm(group)), button("Пригласить", () => { const box = openModal("Пригласить участников"); box.append(el("p", "Отправьте друзьям этот код. Его можно ввести в приложении или отправить боту командой /join.")); const code = field(box, "Код приглашения", "text", group.invite_code); code.readOnly = true; box.append(button("Скопировать", async () => { await navigator.clipboard.writeText(group.invite_code); notify("Код скопирован"); }, "wide")); }, "secondary")); app.append(actions);
  const members = el("details", undefined, "card"); members.append(el("summary", `Участники · ${group.members.length}`)); group.members.forEach(m => members.append(row(m.name, money(group.balances[m.id] || 0, group.currency)))); app.append(members, el("h2", "Траты"));
  const list = el("div"); app.append(list); let offset = 0;
  const load = button("Показать ещё", loadMore, "secondary wide");
  async function loadMore() { const data = await api(`/groups/${id}/expenses?offset=${offset}`); if (version !== generation) return; if (!data.total) list.append(el("div", "Трат пока нет. Добавьте первую общую покупку.", "empty")); data.expenses.forEach(expense => { const card = button("", () => showExpense(group, expense.id), "card expense"); card.append(row(expense.desc, money(expense.amount_cents, group.currency)), el("small", `${group.members.find(m => m.id === expense.payer)?.name || expense.payer} · ${new Date(expense.created_at * 1000).toLocaleDateString("ru-RU")}`)); list.append(card); }); offset += data.expenses.length; load.hidden = offset >= data.total; }
  app.append(load); await loadMore();
}
function expenseForm(group) {
  const box = openModal("Добавить трату"); const desc = field(box, "За что платили?"); desc.maxLength = 500; const total = field(box, `Сумма, ${group.currency}`); total.inputMode = "decimal";
  const payer = select(box, "Кто заплатил", group.members.map(m => [m.id, m.name]), me.id);
  const mode = select(box, "Как разделить", [["equal", "Поровну"], ["custom", "Указать доли"]], "equal");
  box.append(el("p", "Участники", "muted"));
  const parts = group.members.map(member => { const label = el("label", undefined, "participant"); const check = el("input"); check.type = "checkbox"; check.checked = true; const share = el("input"); share.type = "text"; share.inputMode = "decimal"; share.placeholder = "0,00"; share.setAttribute("aria-label", `Доля: ${member.name}`); share.hidden = true; label.append(check, el("span", member.name), share); box.append(label); return {member, check, share}; });
  mode.addEventListener("change", () => parts.forEach(p => { p.share.hidden = mode.value !== "custom"; }));
  const review = el("div"); const operation = crypto.randomUUID();
  box.append(button("Проверить трату", () => {
    const selected = parts.filter(p => p.check.checked); if (!selected.length) throw new Error("Выберите хотя бы одного участника."); if (!desc.value.trim()) throw new Error("Укажите, за что платили.");
    const cents = amount(total.value); const shares = mode.value === "custom" ? selected.map(p => amount(p.share.value, true)) : selected.map((p, i) => Math.floor(cents / selected.length) + (i < cents % selected.length ? 1 : 0));
    if (shares.reduce((sum, value) => sum + value, 0) !== cents) throw new Error("Сумма долей должна совпадать с суммой траты.");
    const payload = {description: desc.value.trim(), amount_cents: cents, payer: Number(payer.value), participants: selected.map(p => p.member.id), shares, operation_id: operation};
    review.replaceChildren(el("h3", "Проверьте перед сохранением"), row(payload.description, money(cents, group.currency)), el("p", `Заплатил(а): ${group.members.find(m => m.id === payload.payer).name}`)); selected.forEach((p, i) => review.append(row(p.member.name, money(shares[i], group.currency))));
    review.append(button("Сохранить трату", async () => { await api(`/groups/${group.id}/expenses`, "POST", payload); modal.close(); notify("Трата сохранена"); await showGroup(group.id); }, "wide")); review.scrollIntoView({block: "nearest"});
  }, "wide"), review);
  box.addEventListener("input", () => review.replaceChildren()); box.addEventListener("change", () => review.replaceChildren());
}
async function showExpense(group, id) { const expense = await api(`/groups/${group.id}/expenses/${id}`); const box = openModal(expense.desc); box.append(el("h2", money(expense.amount_cents, group.currency)), el("p", `Заплатил(а): ${group.members.find(m => m.id === expense.payer)?.name || expense.payer}`)); Object.entries(expense.shares).forEach(([uid, share]) => box.append(row(group.members.find(m => m.id === Number(uid))?.name || uid, money(share, group.currency)))); box.append(el("p", "Редактирование, история и чеки доступны в чате с ботом.", "muted")); if (expense.created_by === me.id) box.append(button("Удалить трату", () => { const confirm = openModal("Удалить эту трату?"); confirm.append(el("p", `${expense.desc} · ${money(expense.amount_cents, group.currency)}. Долги будут пересчитаны. Восстановить трату можно в чате с ботом.`), button("Да, удалить", async () => { await api(`/groups/${group.id}/expenses/${id}`, "DELETE"); modal.close(); await showGroup(group.id); }, "wide danger")); }, "wide danger")); }
async function showDebts() {
  const version = ++generation; currentGroup = null; setTab("debts"); backState(false); const data = await api("/debts"); if (version !== generation) return;
  app.replaceChildren(el("h2", "Долги и платежи"), el("p", "Общий итог по всем вашим группам. Платёж уменьшит долг после подтверждения получателем.", "intro"));
  if (data.pending.length) app.append(el("h3", "Ожидают подтверждения"));
  data.pending.forEach(payment => { const card = el("div", undefined, "card"); card.append(row(`${data.names[payment.from]} → ${data.names[payment.to]}`, money(payment.amount_cents, payment.currency))); const actions = el("div", undefined, "actions"); if (payment.to === me.id) actions.append(button("Деньги получены", () => confirmPayment(payment), "")); actions.append(button(payment.from === me.id ? "Отозвать" : "Отклонить", async () => { await api(`/payments/${payment.batch}/reject`, "POST", {}); await showDebts(); }, "secondary")); card.append(actions); app.append(card); });
  let count = 0;
  Object.entries(data.debts).forEach(([other, byCurrency]) => Object.entries(byCurrency).forEach(([currency, debt]) => { count++; const card = el("div", undefined, "card"); card.append(el("h3", data.names[other]), row(debt.net < 0 ? "Вы должны" : debt.net > 0 ? "Вам должны" : "Взаимозачёт", money(Math.abs(debt.net), currency), debt.net < 0 ? "negative" : "positive")); Object.entries(debt.by_group).forEach(([gid, cents]) => card.append(row(data.groups[gid] || `Группа ${gid}`, money(cents, currency)))); const pending = data.pending.some(p => p.currency === currency && [p.from, p.to].includes(Number(other))); if (debt.net <= 0 && !pending) card.append(button(debt.net === 0 ? "Зафиксировать взаимозачёт" : "Я перевёл деньги", () => paymentForm(Number(other), data.names[other], currency, -debt.net), "wide")); app.append(card); }));
  if (!count && !data.pending.length) app.append(el("div", "Вы в расчёте. Непогашенных долгов нет.", "empty"));
}
function confirmPayment(payment) { const box = openModal("Подтвердить получение?"); box.append(el("p", `Вы подтверждаете получение ${money(payment.amount_cents, payment.currency)}. После этого долг будет уменьшен.`), button("Подтвердить", async () => { await api(`/payments/${payment.batch}/confirm`, "POST", {}); modal.close(); await showDebts(); }, "wide")); }
function paymentForm(other, name, currency, owed) { const box = openModal(owed ? "Записать перевод" : "Взаимозачёт"); box.append(el("p", `Получатель: ${name}. Это запись о платеже; приложение не переводит деньги.`)); const sum = field(box, `Сумма, ${currency}`, "text", (owed / 100).toFixed(2)); sum.inputMode = "decimal"; if (!owed) sum.readOnly = true; box.append(button("Отправить на подтверждение", async () => { const cents = amount(sum.value, !owed); if (cents > owed) throw new Error("Сумма превышает текущий долг."); await api("/payments", "POST", {other, currency, amount_cents: cents}); modal.close(); await showDebts(); }, "wide")); }
const refresh = () => currentTab === "debts" ? showDebts() : currentGroup ? showGroup(currentGroup) : showGroups();
document.querySelector("#groups-tab").onclick = () => showGroups().catch(e => notify(e.message));
document.querySelector("#debts-tab").onclick = () => showDebts().catch(e => notify(e.message));
document.querySelector("#refresh").onclick = () => refresh().catch(e => notify(e.message));
document.querySelector("#close-modal").onclick = () => modal.close();
tg?.BackButton.onClick(() => { if (modal.open) modal.close(); else showGroups().catch(e => notify(e.message)); });
(async () => { tg?.ready(); tg?.expand(); if (!tg?.initData) { app.replaceChildren(el("div", "Откройте «Расходы» через кнопку меню в чате с ботом Telegram.", "empty")); document.querySelector("nav").hidden = true; document.querySelector("#refresh").hidden = true; return; } try { const data = await api("/me"); me = data.user; currencies = data.currencies; await showGroups(); } catch (error) { app.replaceChildren(el("div", error.message, "empty")); } })();
