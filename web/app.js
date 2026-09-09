// Screens and wiring. Everything reusable lives in lib/; the expense form and
// card live in expense.js.
"use strict";

import {money, groupIcon, byDay, dayLabel, amount} from "./lib/money.js";
import {api, cached, remember, forget, pending, flush} from "./lib/api.js";
import {
  el, button, row, field, select, notify, primary, openModal, closeModal, modalOpen,
  ask, onModalClose, skeletons, failure, cardList,
} from "./lib/dom.js";
import {
  tg, locale, supports, haptic, setScreenMain, backState,
  resizeViewport, guardTouches, paintTheme, setMainErrorHandler,
} from "./lib/tg.js";
import {expenseForm, showExpense} from "./expense.js";

const app = document.querySelector("#app");
const cash = (cents, currency) => money(cents, currency, locale);
let me, bot = "", currencies = [], currentGroup = null, currentTab = "groups", request = null;

const ctx = () => ({me, currencies, reload: () => showGroup(currentGroup)});

// -- screen shell ----------------------------------------------------------
// A screen paints from the last known answer at once when it has one, then
// repaints only if the server disagrees. Skeletons are for a cold start.
async function load(path, render) {
  request?.abort();
  const controller = (request = new AbortController());
  const known = cached(path);
  if (known) render(known, controller.signal); else app.replaceChildren(skeletons());
  let data;
  try {
    data = await api(path, "GET", undefined, controller.signal);
  } catch (error) {
    if (controller.signal.aborted) return;
    if (known) notify(error.message);
    else app.replaceChildren(failure(error.message, () => load(path, render)));
    return;
  }
  const unchanged = known && JSON.stringify(known) === JSON.stringify(data);
  remember(path, data);
  if (!unchanged) render(data, controller.signal);
}

function setTab(tab) {
  currentTab = tab;
  ["groups", "debts"].forEach(name => {
    const node = document.querySelector(`#${name}-tab`);
    node.classList.toggle("active", name === tab);
    node.setAttribute("aria-selected", String(name === tab));
  });
}

function heading(text, ...extra) {
  const bar = el("div", undefined, "section-title");
  const tools = el("div", undefined, "tools");
  extra.forEach(node => tools.append(node));
  const reload = button("↻", () => refresh(), "quiet icon-button");
  reload.setAttribute("aria-label", "Обновить");
  tools.append(reload);
  bar.append(el("h2", text), tools);
  return bar;
}

/** Anything still waiting to reach the server, with a way to push it now. */
function queueBanner() {
  const waiting = pending();
  if (!waiting.length) return null;
  const box = el("div", undefined, "card queued");
  box.append(el("span", `${waiting.length} ${waiting.length === 1 ? "трата ждёт" : "траты ждут"} отправки`));
  box.append(button("Отправить", sendQueued, "quiet"));
  return box;
}

async function sendQueued() {
  const {sent, dropped, error} = await flush();
  if (sent) notify(sent === 1 ? "Отложенная трата отправлена" : `Отправлено трат: ${sent}`);
  if (dropped) notify("Часть отложенных трат сервер не принял — они удалены из очереди.");
  if (sent || dropped) await refresh();
  if (error) notify(`${error} Неотправленные траты остаются на устройстве.`);
}

// -- groups ----------------------------------------------------------------
const showGroups = () => {
  currentGroup = null;
  setTab("groups");
  backState(false);
  setScreenMain(null, null, modalOpen());
  return load("/groups", data => {
    app.replaceChildren(heading("Мои группы"), el("p", "Путешествия, дом и всё, что делим вместе.", "intro"));
    const banner = queueBanner();
    if (banner) app.append(banner);
    const actions = el("div", undefined, "actions");
    actions.append(button("+ Создать группу", createGroup), button("Вступить по коду", joinGroup, "secondary"));
    app.append(actions);
    if (!data.groups.length) {
      app.append(el("div", "Пока нет групп. Создайте первую или введите код приглашения от друзей.", "empty"));
      return;
    }
    app.append(cardList(data.groups, group => {
      const standing = group.balance === 0
        ? "Вы в расчёте"
        : group.balance > 0
          ? `Вам должны ${cash(group.balance, group.currency)}`
          : `Ваш долг ${cash(-group.balance, group.currency)}`;
      const card = button("", () => showGroup(group.id), "card group");
      card.setAttribute("aria-label", `${group.title}. ${standing}`);
      const content = el("div");
      content.append(el("span", group.title, "title"), el("small", `${group.currency} · ${standing}`));
      card.append(el("span", groupIcon(group), "icon"), content, el("span", "›", "arrow"));
      return card;
    }));
  });
};

function createGroup() {
  const box = openModal("Новая группа");
  const title = field(box, "Название");
  title.maxLength = 100;
  const currency = select(box, "Валюта группы", currencies.map(code => [code, code]), "RUB");
  primary(box, "Создать", async () => {
    if (!title.value.trim()) throw new Error("Введите название группы.");
    const group = await api("/groups", "POST", {title: title.value, currency: currency.value});
    forget();
    closeModal();
    haptic("success");
    await showGroup(group.id);
  });
}

function joinGroup() {
  const box = openModal("Вступить в группу");
  const code = field(box, "Код приглашения");
  code.maxLength = 64;
  primary(box, "Вступить", async () => {
    const group = await api("/join", "POST", {code: code.value.trim().replace(/^\/join\s+/, "")});
    forget();
    closeModal();
    haptic("success");
    await showGroup(group.id);
  });
}

// -- one group -------------------------------------------------------------
const showGroup = id => {
  currentGroup = id;
  setTab("groups");
  backState(true);
  return load(`/groups/${id}`, (group, signal) => {
    setScreenMain("Добавить трату", async () => expenseForm(group, null, ctx()), modalOpen());
    const named = uid => group.members.find(member => member.id === uid)?.name || uid;
    app.replaceChildren(
      ...(tg?.BackButton ? [] : [button("‹ Все группы", showGroups, "quiet")]),
      heading(group.title),
    );
    const balance = group.balances[me.id] || 0;
    const hero = el("div", undefined, "hero");
    hero.append(
      el("small", balance < 0 ? "Ваш долг в группе" : balance > 0 ? "Вам должны в группе" : "Все расходы учтены"),
      el("strong", balance === 0 ? "Вы в расчёте" : cash(Math.abs(balance), group.currency)),
    );
    app.append(hero);

    const actions = el("div", undefined, "actions");
    if (!tg?.MainButton) actions.append(button("+ Добавить трату", () => expenseForm(group, null, ctx())));
    actions.append(
      button("Пригласить", () => invite(group), "secondary"),
      button("Настройки", () => groupSettings(group), "secondary"),
    );
    app.append(actions);

    // Who owes whom inside this group, already chained. Settling is netted
    // across every shared group, so it happens on the debts tab.
    const members = el("details", undefined, "card");
    members.append(el("summary", `Участники и расчёты · ${group.members.length}`));
    group.members.forEach(member =>
      members.append(row(member.name, cash(group.balances[member.id] || 0, group.currency))));
    if (group.transfers.length) {
      members.append(el("p", "Чтобы закрыть группу:", "muted"));
      group.transfers.forEach(transfer => members.append(
        row(`${named(transfer.from)} → ${named(transfer.to)}`, cash(transfer.amount_cents, group.currency))));
      members.append(el("p", "Платежи проводятся на вкладке «Долги»: там сумма netted по всем вашим общим группам.", "hint"));
    }
    app.append(members, expenses(group, signal));
  });
};

/** The expense list with its own search, payer filter and per-day totals. */
function expenses(group, signal) {
  const section = el("div");
  const bar = el("div", undefined, "filters");
  const search = el("input");
  search.type = "search";
  search.placeholder = "Поиск по названию";
  search.setAttribute("aria-label", "Поиск трат");
  const payer = el("select");
  payer.setAttribute("aria-label", "Кто платил");
  [["0", "Все"], ...group.members.map(member => [String(member.id), member.name])]
    .forEach(([value, name]) => { const option = el("option", name); option.value = value; payer.append(option); });
  bar.append(search, payer);
  const summary = el("p", "", "muted list-summary");
  const list = el("div");
  const more = button("Показать ещё", () => page(), "secondary wide");
  section.append(el("h2", "Траты"), bar, summary, list, more);

  let offset = 0, token = 0, lastDay = null;
  async function page(reset = false) {
    const mine = ++token;
    if (reset) { offset = 0; lastDay = null; list.replaceChildren(); }
    const query = `?offset=${offset}&q=${encodeURIComponent(search.value.trim())}&payer=${payer.value}`;
    const data = await api(`/groups/${group.id}/expenses${query}`, "GET", undefined, signal);
    if (mine !== token) return;
    summary.textContent = data.total
      ? `${data.total} ${plural(data.total, "трата", "траты", "трат")} на ${cash(data.sum_cents, group.currency)}`
      : "";
    if (!data.total) {
      list.replaceChildren(el("div", search.value.trim() || payer.value !== "0"
        ? "Ничего не нашлось. Измените запрос или выберите другого плательщика."
        : "Трат пока нет. Добавьте первую общую покупку.", "empty"));
    }
    byDay(data.expenses).forEach(day => {
      if (day.key !== lastDay?.key) {
        const header = el("div", undefined, "day");
        const subtotal = el("span", "", "amount");
        subtotal.title = "Сумма загруженных трат за день";
        header.append(el("span", dayLabel(day.at, locale)), subtotal);
        list.append(header);
        lastDay = {key: day.key, total: 0, subtotal};
      }
      lastDay.total += day.total;
      lastDay.subtotal.textContent = cash(lastDay.total, group.currency);
      list.append(cardList(day.expenses, expense => {
        const card = button("", () => showExpense(group, expense.id, ctx()), "card expense");
        const paid = group.members.find(member => member.id === expense.payer)?.name || expense.payer;
        const original = expense.orig_currency ? `${cash(expense.orig_amount_cents, expense.orig_currency)} · ` : "";
        card.setAttribute("aria-label", `${expense.desc}, ${cash(expense.amount_cents, group.currency)}, ${paid}`);
        card.append(
          row(expense.desc, cash(expense.amount_cents, group.currency)),
          el("small", `${original}${paid}`),
        );
        return card;
      }));
    });
    offset += data.expenses.length;
    more.hidden = offset >= data.total || !data.expenses.length;
  }
  const rerun = () => page(true).catch(error => { if (!signal.aborted) notify(error.message); });
  search.addEventListener("input", () => { clearTimeout(rerun.timer); rerun.timer = setTimeout(rerun, 300); });
  payer.addEventListener("change", rerun);
  rerun();
  return section;
}

const plural = (count, one, few, many) => {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
};

function invite(group) {
  const box = openModal("Пригласить участников");
  const link = bot ? `https://t.me/${bot}?start=${group.invite_code}` : "";
  box.append(el("p", link
    ? "Отправьте друзьям ссылку — она откроет бота и сразу добавит их в группу. Код ниже работает так же: его можно ввести в приложении или отправить боту командой /join."
    : "Отправьте друзьям этот код. Его можно ввести в приложении или отправить боту командой /join."));
  const code = field(box, "Код приглашения", "text", group.invite_code);
  code.readOnly = true;
  box.append(button("Скопировать код", async () => {
    await navigator.clipboard.writeText(group.invite_code);
    haptic("success");
    notify("Код скопирован");
  }, "secondary wide"));
  // Sharing beats the clipboard inside the Telegram WebView, where copying
  // silently fails on some clients.
  const text = `Делим расходы в группе «${group.title}». Код приглашения: ${group.invite_code}`;
  if (link && tg?.openTelegramLink) {
    primary(box, "Поделиться ссылкой", async () =>
      tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`));
  } else if (navigator.share) {
    primary(box, "Поделиться", async () => navigator.share({text: link ? `${text}\n${link}` : text}).catch(() => {}));
  } else {
    primary(box, "Готово", async () => closeModal());
  }
}

function groupSettings(group) {
  const box = openModal("Настройки группы");
  if (group.is_owner) {
    const title = field(box, "Название", "text", group.title);
    title.maxLength = 100;
    const currency = group.can_change_currency
      ? select(box, "Валюта группы", currencies.map(code => [code, code]), group.currency)
      : null;
    if (!currency) {
      box.append(el("p", `Валюта группы — ${group.currency}. Сменить её можно, только пока в группе нет ни трат, ни платежей: все суммы уже записаны в ней.`, "hint"));
    }
    primary(box, "Сохранить", async () => {
      await api(`/groups/${group.id}/settings`, "POST", {
        title: title.value,
        ...(currency ? {currency: currency.value} : {}),
      });
      forget();
      closeModal();
      haptic("success");
      notify("Настройки сохранены");
      await showGroup(group.id);
    });
  } else {
    box.append(el("p", `Настройки группы меняет её владелец. Валюта — ${group.currency}.`, "hint"));
  }
  box.append(el("h3", "Участники"));
  group.members.forEach(member => {
    box.append(row(member.role === "owner" ? `${member.name} · владелец` : member.name,
      cash(group.balances[member.id] || 0, group.currency)));
    if (!group.is_owner || member.role === "owner") return;
    box.append(button(`Убрать ${member.name}`, () => ask(
      `Убрать ${member.name} из группы? Его прошлые траты останутся в истории.`,
      async () => {
        await api(`/groups/${group.id}/members/${member.id}`, "DELETE");
        forget();
        closeModal();
        haptic("success");
        notify("Участник убран");
        await showGroup(group.id);
      },
    ), "danger wide"));
  });
  box.append(button("Выйти из группы", () => ask(
    "Выйти из группы? Это возможно, только когда вы ни с кем не в долгу внутри неё.",
    async () => {
      const result = await api(`/groups/${group.id}/leave`, "POST", {});
      forget();
      closeModal();
      haptic("success");
      notify(result.deleted ? "Группа удалена: вы были последним участником" : "Вы вышли из группы");
      await showGroups();
    },
  ), "danger wide"));
}

// -- debts -----------------------------------------------------------------
const showDebts = () => {
  currentGroup = null;
  setTab("debts");
  backState(false);
  setScreenMain(null, null, modalOpen());
  return load("/debts", data => {
    const who = id => (Number(id) === me.id ? "Вы" : data.names[id] || id);
    app.replaceChildren(
      heading("Долги и платежи"),
      el("p", "Общий итог по всем вашим группам. Платёж уменьшит долг после подтверждения получателем.", "intro"),
    );
    if (data.pending.length) app.append(el("h3", "Ожидают подтверждения"));
    data.pending.forEach(payment => {
      const card = el("div", undefined, "card");
      card.append(row(`${who(payment.from)} → ${who(payment.to)}`, cash(payment.amount_cents, payment.currency)));
      const actions = el("div", undefined, "actions");
      if (payment.to === me.id) actions.append(button("Деньги получены", () => confirmPayment(payment)));
      actions.append(button(payment.from === me.id ? "Отозвать" : "Отклонить", async () => {
        await api(`/payments/${payment.batch}/reject`, "POST", {});
        forget();
        haptic("success");
        await showDebts();
      }, "secondary"));
      card.append(actions);
      app.append(card);
    });
    let count = 0;
    Object.entries(data.debts).forEach(([other, byCurrency]) => Object.entries(byCurrency).forEach(([currency, debt]) => {
      count++;
      const card = el("div", undefined, "card");
      card.append(
        el("h3", data.names[other]),
        row(debt.net < 0 ? "Вы должны" : debt.net > 0 ? "Вам должны" : "Взаимозачёт",
          cash(Math.abs(debt.net), currency), debt.net < 0 ? "negative" : "positive"),
      );
      Object.entries(debt.by_group).forEach(([gid, cents]) =>
        card.append(row(data.groups[gid] || `Группа ${gid}`, cash(cents, currency))));
      const waiting = data.pending.some(payment =>
        payment.currency === currency && [payment.from, payment.to].includes(Number(other)));
      if (debt.net <= 0 && !waiting) {
        card.append(button(debt.net === 0 ? "Зафиксировать взаимозачёт" : "Я перевёл деньги",
          () => paymentForm(Number(other), data.names[other], currency, -debt.net), "wide"));
      }
      app.append(card);
    }));
    if (!count && !data.pending.length) app.append(el("div", "Вы в расчёте. Непогашенных долгов нет.", "empty"));
  });
};

function confirmPayment(payment) {
  const box = openModal("Подтвердить получение?");
  box.append(el("p", `Вы подтверждаете получение ${cash(payment.amount_cents, payment.currency)}. После этого долг будет уменьшен.`));
  primary(box, "Подтвердить", async () => {
    await api(`/payments/${payment.batch}/confirm`, "POST", {});
    forget();
    closeModal();
    haptic("success");
    await showDebts();
  });
}

function paymentForm(other, name, currency, owed) {
  const box = openModal(owed ? "Записать перевод" : "Взаимозачёт");
  box.append(el("p", `Получатель: ${name}. Это запись о платеже; приложение не переводит деньги.`));
  const sum = field(box, `Сумма, ${currency}`, "text", (owed / 100).toFixed(2));
  sum.inputMode = "decimal";
  if (!owed) sum.readOnly = true;
  primary(box, "Отправить на подтверждение", async () => {
    const cents = amount(sum.value, !owed);
    if (cents > owed) throw new Error("Сумма превышает текущий долг.");
    await api("/payments", "POST", {other, currency, amount_cents: cents});
    forget();
    closeModal();
    haptic("success");
    await showDebts();
  });
}

// -- wiring ----------------------------------------------------------------
const refresh = () => {
  forget();
  return currentTab === "debts" ? showDebts() : currentGroup ? showGroup(currentGroup) : showGroups();
};
document.querySelector("#groups-tab").onclick = () => showGroups();
document.querySelector("#debts-tab").onclick = () => showDebts();
document.querySelector("#close-modal").onclick = () => closeModal();
onModalClose(() => backState(currentGroup !== null));
setMainErrorHandler(error => notify(error.message));
tg?.BackButton.onClick(() => { if (modalOpen()) closeModal(); else if (currentGroup !== null) showGroups(); });
guardTouches(document.querySelector("#modal"));
window.visualViewport?.addEventListener("resize", resizeViewport);
window.visualViewport?.addEventListener("scroll", resizeViewport);
window.addEventListener("resize", resizeViewport);
window.addEventListener("online", () => { sendQueued().catch(() => {}); });
resizeViewport();

(async () => {
  tg?.ready();
  tg?.expand();
  paintTheme();
  tg?.onEvent?.("themeChanged", paintTheme);
  tg?.onEvent?.("viewportChanged", resizeViewport);
  // A vertical swipe inside a form should scroll it, not drag the app shut.
  if (supports("7.7")) { try { tg.disableVerticalSwipes(); } catch { /* client refused */ } }
  if (!tg?.initData) {
    app.replaceChildren(el("div", "Откройте «Расходы» через кнопку меню в чате с ботом Telegram.", "empty"));
    document.querySelector("nav").hidden = true;
    return;
  }
  const start = async () => {
    const data = await api("/me");
    me = data.user;
    bot = data.bot || "";
    currencies = data.currencies;
    if (pending().length) sendQueued().catch(() => {});
    await showGroups();
  };
  try { await start(); } catch (error) { app.replaceChildren(failure(error.message, start)); }
})();
