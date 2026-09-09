"use strict";
const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const modal = document.querySelector("#modal");
const modalBody = document.querySelector("#modal-body");
const locale = (tg?.initDataUnsafe?.user?.language_code || "ru").slice(0, 5);
// A group needs a face of its own: the title picks it when it can, and the
// id keeps the fallback stable across screens.
const ICONS = ["✈️", "🏠", "🍽️", "🎁", "🛒", "🎬", "🚗", "🏖️", "☕", "🎉"];
const ICON_WORDS = [[/поезд|путеш|отпуск|тур|trip|travel/i, "✈️"], [/кварт|дом|дач|ремонт|house|flat/i, "🏠"], [/еда|ужин|обед|рестор|кафе|бар|food/i, "🍽️"], [/подар|празд|рожден|gift/i, "🎁"], [/магаз|покуп|продукт|shop/i, "🛒"], [/море|пляж|beach|курорт/i, "🏖️"], [/маш|авто|такси|car/i, "🚗"], [/офис|работ|проект|команд|team/i, "💼"]];
const groupIcon = group => (ICON_WORDS.find(([pattern]) => pattern.test(group.title)) || [])[1] || ICONS[group.id % ICONS.length];
let me, bot = "", currencies = [], currentGroup = null, currentTab = "groups", generation = 0;
let mainHandler = null, screenMain = null;
const money = (cents, currency) => { const value = cents / 100; const digits = {minimumFractionDigits: 2, maximumFractionDigits: 2}; try { return new Intl.NumberFormat(locale, {style: "currency", currency, ...digits}).format(value); } catch { return `${value.toLocaleString(locale, digits)} ${currency}`; } };
const date = seconds => new Date(seconds * 1000).toLocaleDateString(locale, {day: "numeric", month: "short"});
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
const haptic = kind => { const feedback = tg?.HapticFeedback; if (!feedback) return; try { ["success", "error", "warning"].includes(kind) ? feedback.notificationOccurred(kind) : feedback.impactOccurred(kind || "light"); } catch { /* older clients */ } };
// An open dialog lives in the top layer, so a toast left in the body renders
// behind it — exactly when it carries the error that explains what failed.
function notify(message) { const node = document.querySelector("#notice"); (modal.open ? modal : document.body).append(node); node.textContent = message; node.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => { node.hidden = true; }, 6500); }
function button(text, action, cls = "") {
  const node = el("button", text, cls); node.type = "button";
  node.addEventListener("click", async () => { if (node.disabled) return; node.disabled = true; haptic("light"); try { await action(); } catch (error) { haptic("error"); notify(error.message); } finally { node.disabled = false; } });
  return node;
}
async function api(path, method = "GET", body) {
  let response;
  // A dropped connection rejects with the browser's own English text.
  try { response = await fetch(`/api${path}`, {method, headers: {"Authorization": `tma ${tg?.initData || ""}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})}, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store"}); }
  catch { throw new Error("Нет связи с сервером. Проверьте интернет и повторите."); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Сервер недоступен. Попробуйте ещё раз.");
  return data;
}

// -- Telegram main button --------------------------------------------------
// The primary action of every screen and dialog lives in Telegram's own
// button: it stays pinned above the keyboard and never scrolls away. Without
// a Telegram host (a plain browser) the in-page button is shown instead.
function bindMain(text, action) {
  if (!tg?.MainButton) return false;
  if (mainHandler) tg.MainButton.offClick(mainHandler);
  mainHandler = async () => {
    if (tg.MainButton.isProgressVisible) return;
    haptic("light"); tg.MainButton.showProgress(true);
    try { await action(); } catch (error) { haptic("error"); notify(error.message); } finally { tg.MainButton.hideProgress(); }
  };
  tg.MainButton.setParams({text, is_visible: true, is_active: true});
  tg.MainButton.onClick(mainHandler);
  return true;
}
function unbindMain() { if (mainHandler && tg?.MainButton) { tg.MainButton.offClick(mainHandler); tg.MainButton.hide(); } mainHandler = null; }
function setScreenMain(text, action) { screenMain = text ? {text, action} : null; if (!modal.open) restoreMain(); }
function restoreMain() { if (screenMain) bindMain(screenMain.text, screenMain.action); else unbindMain(); }
function primary(box, text, action) { const node = button(text, action, "wide"); box.append(node); if (bindMain(text, action)) node.hidden = true; return node; }

// Each opening gets its own container, so the listeners a form hangs on its
// box die with it instead of firing inside the next dialog.
function openModal(title) { const box = el("div"); box.append(el("h2", title)); modalBody.replaceChildren(box); unbindMain(); if (!modal.open) modal.showModal(); tg?.BackButton.show(); return box; }
function backState(visible) { if (visible) tg?.BackButton.show(); else tg?.BackButton.hide(); }
// Older clients throw on methods they do not know; showConfirm lands in 6.2.
const supports = version => Boolean(tg?.isVersionAtLeast?.(version));
function ask(question, action) {
  if (supports("6.2")) { tg.showConfirm(question, confirmed => { if (confirmed) action().catch(error => { haptic("error"); notify(error.message); }); }); return; }
  const box = openModal("Подтвердите"); box.append(el("p", question)); primary(box, "Да", action);
}
function field(parent, label, type = "text", value = "") { const wrap = el("label", label); const input = el("input"); input.type = type; input.value = value; input.autocomplete = "off"; wrap.append(input); parent.append(wrap); return input; }
function select(parent, label, options, value) { const wrap = el("label", label); const input = el("select"); options.forEach(([id, name]) => { const option = el("option", name); option.value = id; input.append(option); }); input.value = value; wrap.append(input); parent.append(wrap); return input; }
// People type "1 200,50"; thin spaces come back from copied output too.
function amount(value, allowZero = false) { const text = String(value).replace(/[\s   '’]/g, "").replace(",", "."); if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error("Введите сумму с точностью до копеек."); const [whole, fraction = ""] = text.split("."); const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0")); if (!Number.isSafeInteger(cents) || cents > 1000000000 || cents < (allowZero ? 0 : 1)) throw new Error("Проверьте сумму."); return cents; }
function row(name, value, cls = "") { const node = el("div", undefined, "row"); node.append(el("span", name), el("span", value, `amount ${cls}`)); return node; }
function setTab(tab) { currentTab = tab; ["groups", "debts"].forEach(name => { const node = document.querySelector(`#${name}-tab`); node.classList.toggle("active", name === tab); if (name === tab) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); }); }
function heading(text, ...extra) { const bar = el("div", undefined, "section-title"); const tools = el("div", undefined, "tools"); extra.forEach(node => tools.append(node)); const reload = button("↻", () => refresh(), "quiet icon-button"); reload.setAttribute("aria-label", "Обновить"); tools.append(reload); bar.append(el("h2", text), tools); return bar; }

// -- screen shell ----------------------------------------------------------
// Every screen paints skeletons first, so a tap never leaves the previous
// screen sitting there, and ends in something actionable when it fails.
function skeletons(count = 3) { const box = el("div"); for (let index = 0; index < count; index++) box.append(el("div", undefined, "card skeleton")); return box; }
function failure(message, retry) { const box = el("div", undefined, "empty"); box.append(el("p", message), button("Повторить", retry || (() => refresh()), "")); return box; }
async function load(render) {
  const version = ++generation;
  app.replaceChildren(skeletons());
  try { await render(version); } catch (error) { if (version === generation) app.replaceChildren(failure(error.message)); }
}

const showGroups = () => load(async version => {
  currentGroup = null; setTab("groups"); backState(false); setScreenMain(null);
  const data = await api("/groups"); if (version !== generation) return;
  app.replaceChildren(heading("Мои группы"), el("p", "Путешествия, дом и всё, что делим вместе.", "intro"));
  const actions = el("div", undefined, "actions"); actions.append(button("+ Создать группу", createGroup), button("Вступить по коду", joinGroup, "secondary")); app.append(actions);
  if (!data.groups.length) app.append(el("div", "Пока нет групп. Создайте первую или введите код приглашения от друзей.", "empty"));
  data.groups.forEach(group => { const card = button("", () => showGroup(group.id), "card group"); const content = el("div"); content.append(el("h3", group.title), el("small", `${group.currency} · ${group.balance === 0 ? "Вы в расчёте" : group.balance > 0 ? `Вам должны ${money(group.balance, group.currency)}` : `Ваш долг ${money(-group.balance, group.currency)}`}`)); card.append(el("span", groupIcon(group), "icon"), content, el("span", "›", "arrow")); app.append(card); });
});

function createGroup() { const box = openModal("Новая группа"); const title = field(box, "Название"); title.maxLength = 100; const currency = select(box, "Валюта группы", currencies.map(code => [code, code]), "RUB"); primary(box, "Создать", async () => { if (!title.value.trim()) throw new Error("Введите название группы."); const group = await api("/groups", "POST", {title: title.value, currency: currency.value}); modal.close(); haptic("success"); await showGroup(group.id); }); }
function joinGroup() { const box = openModal("Вступить в группу"); const code = field(box, "Код приглашения"); code.maxLength = 64; primary(box, "Вступить", async () => { const group = await api("/join", "POST", {code: code.value.trim().replace(/^\/join\s+/, "")}); modal.close(); haptic("success"); await showGroup(group.id); }); }

const showGroup = id => load(async version => {
  currentGroup = id; setTab("groups"); backState(true);
  const group = await api(`/groups/${id}`); if (version !== generation) return;
  setScreenMain("Добавить трату", async () => expenseForm(group));
  app.replaceChildren(...(tg?.BackButton ? [] : [button("‹ Все группы", showGroups, "quiet")]), heading(group.title));
  const balance = group.balances[me.id] || 0; const hero = el("div", undefined, "hero"); hero.append(el("small", balance < 0 ? "Ваш долг в группе" : balance > 0 ? "Вам должны в группе" : "Все расходы учтены"), el("strong", balance === 0 ? "Вы в расчёте" : money(Math.abs(balance), group.currency))); app.append(hero);
  const actions = el("div", undefined, "actions");
  if (!tg?.MainButton) actions.append(button("+ Добавить трату", () => expenseForm(group)));
  actions.append(button("Пригласить", () => invite(group), "secondary"), button("Настройки", () => groupSettings(group), "secondary"));
  app.append(actions);
  const members = el("details", undefined, "card"); members.append(el("summary", `Участники · ${group.members.length}`)); group.members.forEach(member => members.append(row(member.name, money(group.balances[member.id] || 0, group.currency)))); app.append(members, el("h2", "Траты"));
  const list = el("div"); app.append(list); let offset = 0;
  const more = button("Показать ещё", loadMore, "secondary wide");
  async function loadMore() {
    const data = await api(`/groups/${id}/expenses?offset=${offset}`); if (version !== generation) return;
    if (!data.total) list.append(el("div", "Трат пока нет. Добавьте первую общую покупку.", "empty"));
    data.expenses.forEach(expense => {
      const card = button("", () => showExpense(group, expense.id), "card expense");
      const paid = group.members.find(member => member.id === expense.payer)?.name || expense.payer;
      const original = expense.orig_currency ? `${money(expense.orig_amount_cents, expense.orig_currency)} · ` : "";
      card.append(row(expense.desc, money(expense.amount_cents, group.currency)), el("small", `${original}${paid} · ${date(expense.created_at)}`));
      list.append(card);
    });
    offset += data.expenses.length;
    more.hidden = offset >= data.total || !data.expenses.length;
  }
  app.append(more); await loadMore();
});

function invite(group) {
  const box = openModal("Пригласить участников");
  const link = bot ? `https://t.me/${bot}?start=${group.invite_code}` : "";
  box.append(el("p", link ? "Отправьте друзьям ссылку — она откроет бота и сразу добавит их в группу. Код ниже работает так же: его можно ввести в приложении или отправить боту командой /join." : "Отправьте друзьям этот код. Его можно ввести в приложении или отправить боту командой /join."));
  const code = field(box, "Код приглашения", "text", group.invite_code); code.readOnly = true;
  box.append(button("Скопировать код", async () => { await navigator.clipboard.writeText(group.invite_code); haptic("success"); notify("Код скопирован"); }, "secondary wide"));
  // Sharing beats the clipboard inside the Telegram WebView, where copying
  // silently fails on some clients.
  const text = `Делим расходы в группе «${group.title}». Код приглашения: ${group.invite_code}`;
  if (link && tg?.openTelegramLink) primary(box, "Поделиться ссылкой", async () => tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`));
  else if (navigator.share) primary(box, "Поделиться", async () => navigator.share({text: link ? `${text}\n${link}` : text}).catch(() => {}));
  else primary(box, "Готово", async () => modal.close());
}

function groupSettings(group) {
  const box = openModal("Настройки группы");
  if (group.is_owner) {
    const title = field(box, "Название", "text", group.title); title.maxLength = 100;
    const currency = group.can_change_currency ? select(box, "Валюта группы", currencies.map(code => [code, code]), group.currency) : null;
    if (!currency) box.append(el("p", `Валюта группы — ${group.currency}. Сменить её можно, только пока в группе нет ни трат, ни платежей: все суммы уже записаны в ней.`, "hint"));
    primary(box, "Сохранить", async () => { await api(`/groups/${group.id}/settings`, "POST", {title: title.value, ...(currency ? {currency: currency.value} : {})}); modal.close(); haptic("success"); notify("Настройки сохранены"); await showGroup(group.id); });
  } else box.append(el("p", `Настройки группы меняет её владелец. Валюта — ${group.currency}.`, "hint"));
  box.append(el("h3", "Участники"));
  group.members.forEach(member => {
    box.append(row(member.role === "owner" ? `${member.name} · владелец` : member.name, money(group.balances[member.id] || 0, group.currency)));
    if (group.is_owner && member.role !== "owner") box.append(button(`Убрать ${member.name}`, () => ask(`Убрать ${member.name} из группы? Его прошлые траты останутся в истории.`, async () => { await api(`/groups/${group.id}/members/${member.id}`, "DELETE"); modal.close(); haptic("success"); notify("Участник убран"); await showGroup(group.id); }), "danger wide"));
  });
  box.append(button("Выйти из группы", () => ask("Выйти из группы? Это возможно, только когда вы ни с кем не в долгу внутри неё.", async () => { const result = await api(`/groups/${group.id}/leave`, "POST", {}); modal.close(); haptic("success"); notify(result.deleted ? "Группа удалена: вы были последним участником" : "Вы вышли из группы"); await showGroups(); }), "danger wide"));
}

// -- expense form ----------------------------------------------------------
const equalShares = (cents, count) => Array.from({length: count}, (_, index) => Math.floor(cents / count) + (index < cents % count ? 1 : 0));
function partShares(cents, parts) {
  const total = parts.reduce((sum, value) => sum + value, 0);
  if (total <= 0) throw new Error("Укажите хотя бы одну долю больше нуля.");
  const shares = parts.map(part => Math.floor((cents * part) / total));
  let rest = cents - shares.reduce((sum, value) => sum + value, 0);
  const order = parts.map((_, index) => index).sort((a, b) => parts[b] - parts[a] || a - b);
  for (let index = 0; rest > 0; index++, rest--) shares[order[index % order.length]]++;
  return shares;
}
function expenseForm(group, existing) {
  const box = openModal(existing ? "Изменить трату" : "Добавить трату");
  const desc = field(box, "За что платили?", "text", existing?.desc || ""); desc.maxLength = 500;
  const startCurrency = existing?.orig_currency || group.currency;
  const startAmount = existing ? (existing.orig_currency ? existing.orig_amount_cents : existing.amount_cents) / 100 : "";
  const total = field(box, "Сумма", "text", startAmount === "" ? "" : startAmount.toFixed(2)); total.inputMode = "decimal";
  const currency = select(box, "Валюта", currencies.map(code => [code, code]), startCurrency);
  const conversion = el("div"); box.append(conversion);
  let converted = null;
  const payer = select(box, "Кто заплатил", group.members.map(member => [member.id, member.name]), existing?.payer ?? me.id);
  const mode = select(box, "Как разделить", [["equal", "Поровну"], ["parts", "По долям"], ["custom", "Указать суммы"]], "equal");
  const chips = el("div", undefined, "chips");
  const quick = (text, action) => button(text, () => { action(); update(); }, "chip");
  box.append(el("p", "Участники", "muted"), chips);
  const parts = group.members.map(member => {
    const label = el("label", undefined, "participant"); const check = el("input"); check.type = "checkbox";
    check.checked = existing ? String(member.id) in existing.shares : true;
    const share = el("input"); share.type = "text"; share.inputMode = "decimal"; share.placeholder = "0,00"; share.setAttribute("aria-label", `Доля: ${member.name}`); share.hidden = true;
    if (existing?.shares[member.id] !== undefined) share.value = (existing.shares[member.id] / 100).toFixed(2);
    label.append(check, el("span", member.name), share); box.append(label);
    return {member, check, share};
  });
  chips.append(quick("Все", () => parts.forEach(part => { part.check.checked = true; })), quick("Никого", () => parts.forEach(part => { part.check.checked = false; })), quick("Только я", () => parts.forEach(part => { part.check.checked = part.member.id === me.id; })), quick("Я и плательщик", () => parts.forEach(part => { part.check.checked = [me.id, Number(payer.value)].includes(part.member.id); })));
  const tally = el("p", "", "tally"); box.append(tally);
  const review = el("div"); const operation = crypto.randomUUID().replace(/-/g, "");

  function baseAmount() { return amount((converted || total).value); }
  function plan() {
    const selected = parts.filter(part => part.check.checked);
    if (!selected.length) throw new Error("Выберите хотя бы одного участника.");
    const cents = baseAmount();
    if (mode.value === "equal") return {selected, cents, shares: equalShares(cents, selected.length)};
    if (mode.value === "parts") return {selected, cents, shares: partShares(cents, selected.map(part => { const value = Number(part.share.value.replace(",", ".")); if (!Number.isFinite(value) || value < 0 || value > 1000) throw new Error("Доля — число от 0 до 1000."); return value; }))};
    return {selected, cents, shares: selected.map(part => amount(part.share.value, true))};
  }
  // A running total beats an error after the fact: the gap is visible while
  // the numbers are still being typed.
  function update() {
    parts.forEach(part => { part.share.hidden = mode.value === "equal"; if (mode.value === "parts" && !part.share.dataset.parts) { part.share.value = "1"; part.share.placeholder = "1"; part.share.dataset.parts = "1"; } if (mode.value === "custom" && part.share.dataset.parts) { part.share.value = ""; part.share.placeholder = "0,00"; delete part.share.dataset.parts; } });
    review.replaceChildren(); bindMain(checkLabel, check);
    let state;
    if (!total.value.trim()) { tally.textContent = "Укажите сумму — покажу, как она разделится."; tally.className = "tally"; return; }
    try { state = plan(); } catch (error) { tally.textContent = error.message; tally.className = "tally warn"; return; }
    const sum = state.shares.reduce((value, share) => value + share, 0);
    const rest = state.cents - sum;
    tally.className = `tally ${rest === 0 ? "ok" : "warn"}`;
    tally.textContent = rest === 0
      ? `Распределено ${money(sum, group.currency)} на ${state.selected.length} чел. — сходится.`
      : `Распределено ${money(sum, group.currency)} из ${money(state.cents, group.currency)}. ${rest > 0 ? `Осталось ${money(rest, group.currency)}` : `Перебор на ${money(-rest, group.currency)}`}.`;
  }

  async function refreshRate() {
    if (!converted) return;
    const hint = conversion.querySelector(".hint");
    let cents; try { cents = amount(total.value); } catch { hint.textContent = "Введите сумму в валюте траты — курс подставится сам."; return; }
    hint.textContent = "Смотрим курс дня…";
    try {
      const data = await api(`/rate?from=${encodeURIComponent(currency.value)}&to=${encodeURIComponent(group.currency)}&amount=${cents}`);
      if (data.converted == null) { hint.textContent = "Курс недоступен — впишите сумму, которую списал банк."; return; }
      if (!converted.dataset.touched) { converted.value = (data.converted / 100).toFixed(2); update(); }
      hint.textContent = `Курс дня: 1 ${currency.value} ≈ ${data.rate.toFixed(4)} ${group.currency}. Замените на сумму, которую списал банк, если она другая.`;
    } catch { hint.textContent = "Курс недоступен — впишите сумму, которую списал банк."; }
  }
  function retarget() {
    const foreign = currency.value !== group.currency;
    conversion.replaceChildren(); converted = null;
    total.parentElement.firstChild.textContent = `Сумма, ${currency.value}`;
    if (!foreign) { update(); return; }
    converted = field(conversion, `Списано в валюте группы, ${group.currency}`);
    converted.inputMode = "decimal";
    if (existing?.orig_currency === currency.value) { converted.value = (existing.amount_cents / 100).toFixed(2); converted.dataset.touched = "1"; }
    converted.addEventListener("input", () => { converted.dataset.touched = "1"; });
    conversion.append(el("p", "", "hint"));
    update(); refreshRate();
  }

  const checkLabel = "Проверить трату";
  function check() {
    const state = plan();
    if (!desc.value.trim()) throw new Error("Укажите, за что платили.");
    if (state.shares.reduce((sum, share) => sum + share, 0) !== state.cents) throw new Error("Сумма долей должна совпадать с суммой траты.");
    const foreign = currency.value !== group.currency;
    const payload = {description: desc.value.trim(), amount_cents: state.cents, payer: Number(payer.value), participants: state.selected.map(part => part.member.id), shares: state.shares, ...(foreign ? {orig_currency: currency.value, orig_amount_cents: amount(total.value)} : {})};
    review.replaceChildren(el("h3", "Проверьте перед сохранением"), row(payload.description, money(state.cents, group.currency)));
    if (foreign) review.append(el("p", `Заплачено ${money(payload.orig_amount_cents, currency.value)} — в ледже́р уйдёт ${money(state.cents, group.currency)}.`, "hint"));
    review.append(el("p", `Заплатил(а): ${group.members.find(member => member.id === payload.payer).name}`));
    state.selected.forEach((part, index) => review.append(row(part.member.name, money(state.shares[index], group.currency))));
    primary(review, existing ? "Сохранить изменения" : "Сохранить трату", async () => {
      if (existing) await api(`/groups/${group.id}/expenses/${existing.id}`, "POST", {...payload, revision: existing.revision});
      else await api(`/groups/${group.id}/expenses`, "POST", {...payload, operation_id: operation});
      modal.close(); haptic("success"); notify(existing ? "Трата обновлена" : "Трата сохранена"); await showGroup(group.id);
    });
    review.scrollIntoView({block: "nearest"});
  }

  primary(box, checkLabel, check); box.append(review);
  currency.addEventListener("change", retarget);
  total.addEventListener("input", () => { update(); clearTimeout(refreshRate.timer); refreshRate.timer = setTimeout(refreshRate, 500); });
  box.addEventListener("input", event => { if (event.target !== total) update(); });
  box.addEventListener("change", event => { if (event.target !== currency) update(); });
  retarget();
}

async function showExpense(group, id) {
  const expense = await api(`/groups/${group.id}/expenses/${id}`);
  const box = openModal(expense.desc);
  box.append(el("h2", money(expense.amount_cents, group.currency)));
  if (expense.orig_currency) box.append(el("p", `Заплачено ${money(expense.orig_amount_cents, expense.orig_currency)} по курсу дня.`, "hint"));
  box.append(el("p", `Заплатил(а): ${group.members.find(member => member.id === expense.payer)?.name || expense.payer} · ${date(expense.created_at)}`));
  Object.entries(expense.shares).forEach(([uid, share]) => box.append(row(group.members.find(member => member.id === Number(uid))?.name || uid, money(share, group.currency))));
  box.append(el("p", "История правок и чеки — в чате с ботом.", "muted"));
  if (expense.can_edit) {
    primary(box, "Изменить", async () => expenseForm(group, expense));
    box.append(button("Удалить трату", () => ask(`Удалить «${expense.desc}» на ${money(expense.amount_cents, group.currency)}? Долги пересчитаются, а восстановить трату можно в чате с ботом.`, async () => { await api(`/groups/${group.id}/expenses/${id}`, "DELETE"); modal.close(); haptic("success"); await showGroup(group.id); }), "wide danger"));
  }
}

const showDebts = () => load(async version => {
  currentGroup = null; setTab("debts"); backState(false); setScreenMain(null);
  const data = await api("/debts"); if (version !== generation) return;
  const who = id => (Number(id) === me.id ? "Вы" : data.names[id] || id);
  app.replaceChildren(heading("Долги и платежи"), el("p", "Общий итог по всем вашим группам. Платёж уменьшит долг после подтверждения получателем.", "intro"));
  if (data.pending.length) app.append(el("h3", "Ожидают подтверждения"));
  data.pending.forEach(payment => {
    const card = el("div", undefined, "card");
    card.append(row(`${who(payment.from)} → ${who(payment.to)}`, money(payment.amount_cents, payment.currency)));
    const actions = el("div", undefined, "actions");
    if (payment.to === me.id) actions.append(button("Деньги получены", () => confirmPayment(payment)));
    actions.append(button(payment.from === me.id ? "Отозвать" : "Отклонить", async () => { await api(`/payments/${payment.batch}/reject`, "POST", {}); haptic("success"); await showDebts(); }, "secondary"));
    card.append(actions); app.append(card);
  });
  let count = 0;
  Object.entries(data.debts).forEach(([other, byCurrency]) => Object.entries(byCurrency).forEach(([currency, debt]) => {
    count++;
    const card = el("div", undefined, "card");
    card.append(el("h3", data.names[other]), row(debt.net < 0 ? "Вы должны" : debt.net > 0 ? "Вам должны" : "Взаимозачёт", money(Math.abs(debt.net), currency), debt.net < 0 ? "negative" : "positive"));
    Object.entries(debt.by_group).forEach(([gid, cents]) => card.append(row(data.groups[gid] || `Группа ${gid}`, money(cents, currency))));
    const pending = data.pending.some(payment => payment.currency === currency && [payment.from, payment.to].includes(Number(other)));
    if (debt.net <= 0 && !pending) card.append(button(debt.net === 0 ? "Зафиксировать взаимозачёт" : "Я перевёл деньги", () => paymentForm(Number(other), data.names[other], currency, -debt.net), "wide"));
    app.append(card);
  }));
  if (!count && !data.pending.length) app.append(el("div", "Вы в расчёте. Непогашенных долгов нет.", "empty"));
});

function confirmPayment(payment) { const box = openModal("Подтвердить получение?"); box.append(el("p", `Вы подтверждаете получение ${money(payment.amount_cents, payment.currency)}. После этого долг будет уменьшен.`)); primary(box, "Подтвердить", async () => { await api(`/payments/${payment.batch}/confirm`, "POST", {}); modal.close(); haptic("success"); await showDebts(); }); }
function paymentForm(other, name, currency, owed) { const box = openModal(owed ? "Записать перевод" : "Взаимозачёт"); box.append(el("p", `Получатель: ${name}. Это запись о платеже; приложение не переводит деньги.`)); const sum = field(box, `Сумма, ${currency}`, "text", (owed / 100).toFixed(2)); sum.inputMode = "decimal"; if (!owed) sum.readOnly = true; primary(box, "Отправить на подтверждение", async () => { const cents = amount(sum.value, !owed); if (cents > owed) throw new Error("Сумма превышает текущий долг."); await api("/payments", "POST", {other, currency, amount_cents: cents}); modal.close(); haptic("success"); await showDebts(); }); }

const refresh = () => currentTab === "debts" ? showDebts() : currentGroup ? showGroup(currentGroup) : showGroups();
document.querySelector("#groups-tab").onclick = () => showGroups();
document.querySelector("#debts-tab").onclick = () => showDebts();
document.querySelector("#close-modal").onclick = () => modal.close();
modal.addEventListener("close", () => { modalBody.replaceChildren(); restoreMain(); backState(currentGroup !== null); });
tg?.BackButton.onClick(() => { if (modal.open) modal.close(); else if (currentGroup !== null) showGroups(); });

function paintTheme() { document.querySelector('meta[name=theme-color]').setAttribute("content", getComputedStyle(document.body).backgroundColor); if (!supports("6.1")) return; try { tg.setHeaderColor("secondary_bg_color"); tg.setBackgroundColor("secondary_bg_color"); } catch { /* the client refused the colour */ } }

(async () => {
  tg?.ready(); tg?.expand(); paintTheme(); tg?.onEvent?.("themeChanged", paintTheme);
  if (!tg?.initData) { app.replaceChildren(el("div", "Откройте «Расходы» через кнопку меню в чате с ботом Telegram.", "empty")); document.querySelector("nav").hidden = true; return; }
  const start = async () => { const data = await api("/me"); me = data.user; bot = data.bot || ""; currencies = data.currencies; await showGroups(); };
  try { await start(); } catch (error) { app.replaceChildren(failure(error.message, start)); }
})();
