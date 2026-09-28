// The expense form and the expense card.
//
// The form reads every control into one plain state object and derives
// everything else from it in one place, so the running total, the review and
// the share inputs can never disagree about what was typed.
"use strict";

import {amount, money, equalShares, partShares} from "./lib/money.js";
import {api, apiFile, enqueue, forget} from "./lib/api.js";
import {el, button, row, field, select, primary, openModal, closeModal, ask, notify} from "./lib/dom.js";
import {haptic, locale, bindMain} from "./lib/tg.js";

const CHECK_LABEL = "Проверить трату";
const cash = (cents, currency) => money(cents, currency, locale);
const day = seconds => new Date(seconds * 1000).toLocaleDateString(locale, {day: "numeric", month: "short"});

export function expenseForm(group, existing, ctx) {
  const {me, currencies, reload} = ctx;
  const box = openModal(existing ? "Изменить трату" : "Добавить трату");
  const operation = crypto.randomUUID().replace(/-/g, "");

  const desc = field(box, "За что платили?", "text", existing?.desc || "");
  desc.maxLength = 500;
  const startCurrency = existing?.orig_currency || group.currency;
  const startCents = existing ? (existing.orig_currency ? existing.orig_amount_cents : existing.amount_cents) : 0;
  const total = field(box, `Сумма, ${startCurrency}`, "text", startCents ? (startCents / 100).toFixed(2) : "");
  total.inputMode = "decimal";
  const currency = select(box, "Валюта", currencies.map(code => [code, code]), startCurrency);
  const conversion = el("div");
  box.append(conversion);
  let converted = null, shownCurrency = null, rateRequest = 0;

  const payer = select(box, "Кто заплатил", group.members.map(member => [member.id, member.name]), existing?.payer ?? me.id);
  const mode = select(box, "Как разделить", [["equal", "Поровну"], ["parts", "По долям"], ["custom", "Указать суммы"]], existing ? "custom" : "equal");
  const chips = el("div", undefined, "chips");
  chips.append(button("Все", () => { parts.forEach(part => { part.check.checked = true; }); sync(); }, "chip"));
  box.append(el("p", "Участники", "muted"), chips);

  const parts = group.members.map(member => {
    const label = el("label", undefined, "participant");
    const check = el("input");
    check.type = "checkbox";
    check.checked = existing ? String(member.id) in existing.shares : true;
    const share = el("input");
    share.type = "text";
    share.inputMode = "decimal";
    share.setAttribute("aria-label", `Доля: ${member.name}`);
    share.hidden = true;
    if (existing?.shares[member.id] !== undefined) share.value = (existing.shares[member.id] / 100).toFixed(2);
    label.append(check, el("span", member.name), share);
    box.append(label);
    return {member, check, share};
  });

  // The total follows the form down the screen: on a phone the participant
  // list alone is taller than the visible dialog.
  const tally = el("p", "", "tally");
  const review = el("div");
  box.append(tally);
  primary(box, CHECK_LABEL, check);
  box.append(review);

  function readState() {
    const foreign = currency.value !== group.currency;
    return {
      desc: desc.value.trim(),
      currency: currency.value,
      foreign,
      typed: total.value,
      converted: converted?.value ?? "",
      payer: Number(payer.value),
      mode: mode.value,
      picks: parts.filter(part => part.check.checked),
    };
  }

  // Never throws: an unfinished form is a state to describe, not an error.
  function plan(state) {
    if (!state.picks.length) return {error: "Выберите хотя бы одного участника."};
    const source = state.foreign ? state.converted : state.typed;
    if (!source.trim()) return {waiting: "Укажите сумму — покажу, как она разделится."};
    let cents;
    try { cents = amount(source); } catch (error) { return {error: error.message}; }
    const count = state.picks.length;
    if (state.mode === "equal") return {cents, shares: equalShares(cents, count)};
    if (state.mode === "parts") {
      const weights = [];
      for (const part of state.picks) {
        const value = Number(part.share.value.replace(",", "."));
        if (!Number.isFinite(value) || value < 0 || value > 1000) return {cents, error: "Доля — число от 0 до 1000."};
        weights.push(value);
      }
      try { return {cents, shares: partShares(cents, weights)}; }
      catch (error) { return {cents, error: error.message}; }
    }
    const shares = [];
    for (const part of state.picks) {
      // A blank field in this mode means nothing yet, not a typo.
      if (!part.share.value.trim()) { shares.push(0); continue; }
      try { shares.push(amount(part.share.value, true)); } catch (error) { return {cents, error: error.message}; }
    }
    return {cents, shares};
  }

  function render(state) {
    if (state.currency !== shownCurrency) {
      shownCurrency = state.currency;
      total.parentElement.firstChild.textContent = `Сумма, ${state.currency}`;
      conversion.replaceChildren();
      converted = null;
      if (state.foreign) {
        converted = field(conversion, `Списано в валюте группы, ${group.currency}`);
        converted.inputMode = "decimal";
        if (existing?.orig_currency === state.currency) {
          converted.value = (existing.amount_cents / 100).toFixed(2);
          converted.dataset.touched = "1";
        }
        converted.addEventListener("input", () => { converted.dataset.touched = "1"; });
        conversion.append(el("p", "", "hint"));
      }
    }
    parts.forEach(part => {
      part.share.hidden = state.mode === "equal";
      part.share.placeholder = state.mode === "parts" ? "1" : "0,00";
      if (state.mode === "parts" && !part.share.dataset.parts) { part.share.value = "1"; part.share.dataset.parts = "1"; }
      if (state.mode !== "parts" && part.share.dataset.parts) { part.share.value = ""; delete part.share.dataset.parts; }
    });
    review.replaceChildren();
    bindMain(CHECK_LABEL, check);

    const outcome = plan({...state, converted: converted?.value ?? ""});
    if (outcome.waiting) { tally.textContent = outcome.waiting; tally.className = "tally"; return; }
    if (outcome.error && !outcome.shares) { tally.textContent = outcome.error; tally.className = "tally warn"; return; }
    const sum = outcome.shares.reduce((value, share) => value + share, 0);
    const rest = outcome.cents - sum;
    tally.className = `tally ${rest === 0 ? "ok" : "warn"}`;
    tally.textContent = rest === 0
      ? `Распределено ${cash(sum, group.currency)} на ${state.picks.length} чел. — сходится.`
      : `Распределено ${cash(sum, group.currency)} из ${cash(outcome.cents, group.currency)}. `
        + `${rest > 0 ? `Осталось ${cash(rest, group.currency)}` : `Перебор на ${cash(-rest, group.currency)}`}.`;
  }

  const sync = () => render(readState());

  async function refreshRate() {
    const mine = ++rateRequest;
    if (!converted || !box.isConnected) return;
    const target = converted, sourceCurrency = currency.value, typed = total.value;
    const current = () => mine === rateRequest && box.isConnected && converted === target
      && currency.value === sourceCurrency && total.value === typed;
    const hint = conversion.querySelector(".hint");
    let cents;
    try { cents = amount(typed); } catch { hint.textContent = "Введите сумму в валюте траты — курс подставится сам."; return; }
    hint.textContent = "Смотрим курс дня…";
    try {
      const data = await api(`/rate?from=${encodeURIComponent(sourceCurrency)}&to=${encodeURIComponent(group.currency)}&amount=${cents}`);
      if (!current()) return;
      if (data.converted == null) { hint.textContent = "Курс недоступен — впишите сумму, которую списал банк."; return; }
      if (!converted.dataset.touched) { converted.value = (data.converted / 100).toFixed(2); sync(); }
      hint.textContent = `Курс дня: 1 ${currency.value} ≈ ${data.rate.toFixed(4)} ${group.currency}. `
        + "Замените на сумму, которую списал банк, если она другая.";
    } catch { if (current()) hint.textContent = "Курс недоступен — впишите сумму, которую списал банк."; }
  }

  function check() {
    const state = readState();
    const outcome = plan(state);
    if (outcome.waiting) throw new Error("Укажите сумму траты.");
    if (outcome.error) throw new Error(outcome.error);
    if (!state.desc) throw new Error("Укажите, за что платили.");
    if (outcome.shares.reduce((sum, share) => sum + share, 0) !== outcome.cents) {
      throw new Error("Сумма долей должна совпадать с суммой траты.");
    }
    const payload = {
      description: state.desc,
      amount_cents: outcome.cents,
      payer: state.payer,
      participants: state.picks.map(part => part.member.id),
      shares: outcome.shares,
      ...(state.foreign ? {orig_currency: state.currency, orig_amount_cents: amount(state.typed)} : {}),
    };
    review.replaceChildren(el("h3", "Проверьте перед сохранением"), row(payload.description, cash(outcome.cents, group.currency)));
    if (state.foreign) {
      review.append(el("p", `Заплачено ${cash(payload.orig_amount_cents, state.currency)} — в группу пойдёт ${cash(outcome.cents, group.currency)}.`, "hint"));
    }
    review.append(el("p", `Заплатил(а): ${group.members.find(member => member.id === payload.payer).name}`));
    state.picks.forEach((part, index) => review.append(row(part.member.name, cash(outcome.shares[index], group.currency))));
    primary(review, existing ? "Сохранить изменения" : "Сохранить трату", () => save(payload));
    review.scrollIntoView({block: "nearest"});
  }

  async function save(payload) {
    if (ctx.saveQueued) {
      await ctx.saveQueued(payload);
      closeModal();
      notify("Изменения сохранены в очереди");
      await reload();
      return;
    }
    const path = existing ? `/groups/${group.id}/expenses/${existing.id}` : `/groups/${group.id}/expenses`;
    const body = existing ? {...payload, revision: existing.revision}
      : {...payload, operation_id: operation, group_currency: group.currency};
    try {
      await api(path, "POST", body);
    } catch (error) {
      // A create carries an idempotent operation_id, so replaying it later is
      // safe. An edit is tied to a revision that will have moved on.
      if (!error.offline || existing) throw error;
      enqueue({path, body, group_title: group.title, currency: group.currency});
      closeModal();
      notify("Нет связи — трата сохранена на устройстве и уйдёт, как только связь появится.");
      await reload();
      return;
    }
    forget();
    await ctx.afterSave?.();
    closeModal();
    haptic("success");
    notify(existing ? "Трата обновлена" : "Трата сохранена");
    await reload();
  }

  currency.addEventListener("change", () => { clearTimeout(refreshRate.timer); sync(); refreshRate(); });
  total.addEventListener("input", () => {
    // Invalidate immediately, including the debounce window. An automatic
    // conversion belongs to the previous amount until a new answer arrives.
    rateRequest++;
    if (converted && !converted.dataset.touched) converted.value = "";
    sync();
    clearTimeout(refreshRate.timer);
    refreshRate.timer = setTimeout(refreshRate, 500);
  });
  box.addEventListener("input", event => { if (event.target !== total) sync(); });
  box.addEventListener("change", event => { if (event.target !== currency) sync(); });
  sync();
  refreshRate();
}

export async function showExpense(group, id, ctx) {
  const expense = await api(`/groups/${group.id}/expenses/${id}`);
  const box = openModal(expense.desc);
  box.append(el("h2", cash(expense.amount_cents, group.currency)));
  if (expense.orig_currency) {
    box.append(el("p", `Заплачено ${cash(expense.orig_amount_cents, expense.orig_currency)} по курсу дня.`, "hint"));
  }
  const paid = group.members.find(member => member.id === expense.payer)?.name || expense.payer;
  box.append(el("p", `Заплатил(а): ${paid} · ${day(expense.created_at)}`));
  Object.entries(expense.shares).forEach(([uid, share]) =>
    box.append(row(group.members.find(member => member.id === Number(uid))?.name || uid, cash(share, group.currency))));
  box.append(button("История изменений", () => showHistory(group, expense, ctx), "secondary wide"));
  receipts(box, group, expense, ctx);
  if (expense.deleted) {
    box.append(el("p", "Трата удалена и не участвует в расчёте долгов.", "hint"));
    if (expense.can_restore) primary(box, "Восстановить трату", () => restoreExpense(group, id, ctx));
    else box.append(el("p", "Восстановить трату может её автор.", "hint"));
    return;
  }
  if (!expense.can_edit) return;
  primary(box, "Изменить", async () => expenseForm(group, expense, ctx));
  box.append(button("Удалить трату", () => ask(
    `Удалить «${expense.desc}» на ${cash(expense.amount_cents, group.currency)}? Долги пересчитаются. Трату можно будет восстановить.`,
    async () => {
      await api(`/groups/${group.id}/expenses/${id}`, "DELETE");
      forget(); closeModal(); haptic("success"); await ctx.reload();
      const undo = openModal("Трата удалена");
      undo.append(el("p", `«${expense.desc}» больше не влияет на долги. Позже её можно найти в группе через фильтр «Удалённые».`));
      primary(undo, "Восстановить трату", () => restoreExpense(group, id, ctx));
    },
  ), "wide danger"));
}

async function restoreExpense(group, id, ctx) {
  await api(`/groups/${group.id}/expenses/${id}/restore`, "POST", {});
  forget(); closeModal(); haptic("success"); await ctx.reload();
  await showExpense(group, id, ctx);
  notify("Трата восстановлена");
}

function receipts(box, group, expense, ctx) {
  const section = el("section", undefined, "receipt-section");
  section.append(el("h3", "Чек"));
  const preview = el("div");
  const path = `/groups/${group.id}/expenses/${expense.id}/receipt`;
  if (expense.has_receipt) section.append(button("Посмотреть чек", async () => {
    const {blob} = await apiFile(path);
    if (!section.isConnected) return;
    const img = el("img", undefined, "receipt-image"); img.alt = `Чек: ${expense.desc}`;
    const url = URL.createObjectURL(blob);
    img.onload = () => { URL.revokeObjectURL(url); img.scrollIntoView({block: "nearest"}); };
    img.onerror = () => { URL.revokeObjectURL(url); notify("Не удалось показать фото. Попробуйте открыть чек ещё раз."); };
    img.src = url; preview.replaceChildren(img);
  }, "secondary wide"));
  else section.append(el("p", "Чек не прикреплён.", "hint"));
  section.append(preview);
  if (expense.can_edit) {
    const file = el("input");
    file.type = "file";
    file.accept = "image/jpeg,image/png";
    file.hidden = true;
    file.setAttribute("aria-label", "Фото чека");
    const label = expense.has_receipt ? "Заменить чек" : "Прикрепить чек";
    const attach = el("button", label, "wide");
    attach.type = "button";
    let uploading = false;
    attach.addEventListener("click", () => {
      if (uploading) return;
      haptic("light");
      // Keep the picker in the click gesture; clearing lets the same photo
      // trigger change again after an unsuccessful upload.
      file.value = "";
      file.click();
    });
    file.addEventListener("change", async () => {
      const photo = file.files?.[0];
      if (!photo || uploading) return;
      uploading = true;
      attach.disabled = true;
      attach.textContent = "Загружаем чек…";
      attach.setAttribute("aria-busy", "true");
      try {
        if (!["image/jpeg", "image/png"].includes(photo.type)) throw new Error("Выберите фото JPEG или PNG.");
        if (photo.size > 10 * 1024 * 1024) throw new Error("Фото должно быть не больше 10 МБ.");
        await api(`${path}?revision=${expense.revision}`, "POST", photo);
        forget(); await ctx.reload();
        if (section.isConnected) await showExpense(group, expense.id, ctx);
        haptic("success");
        notify("Чек сохранён в Telegram и прикреплён к трате");
      } catch (error) {
        haptic("error"); notify(error.message);
      } finally {
        uploading = false;
        attach.disabled = false;
        attach.textContent = label;
        attach.removeAttribute("aria-busy");
      }
    });
    section.append(file, attach,
      el("p", "Выберите фото — оно загрузится автоматически и появится в вашем чате с ботом Telegram. Участники группы смогут посмотреть его здесь. JPEG или PNG, до 10 МБ.", "hint"));
    if (expense.has_receipt) section.append(button("Убрать чек из траты", () => ask(
      "Убрать чек из траты? Фото останется в вашем чате Telegram.", async () => {
        await api(`${path}?revision=${expense.revision}`, "DELETE");
        forget(); await ctx.reload(); await showExpense(group, expense.id, ctx);
      },
    ), "danger wide"));
  }
  box.append(section);
}

async function showHistory(group, expense, ctx) {
  const box = openModal("История изменений");
  box.append(button("‹ К трате", () => showExpense(group, expense.id, ctx), "quiet"));
  const list = el("div"); box.append(list);
  let offset = 0;
  const titles = {create: "Трата добавлена", edit: "Трата изменена", delete: "Трата удалена", restore: "Трата восстановлена", receipt: "Чек обновлён"};
  async function load() {
    const data = await api(`/groups/${group.id}/expenses/${expense.id}/history?offset=${offset}`);
    if (!box.isConnected) return;
    const name = uid => data.names[uid] || String(uid);
    for (const event of data.history) {
      const card = el("div", undefined, "card history-event");
      card.append(el("h3", titles[event.action] || "Изменение"),
        el("p", `${name(event.actor)} · ${new Date(event.created_at * 1000).toLocaleString(locale)}`, "muted"));
      const before = event.before, after = event.after;
      const change = (label, oldValue, newValue) => {
        if (oldValue !== newValue) card.append(row(label, oldValue === undefined ? String(newValue) : `${oldValue} → ${newValue}`));
      };
      change("Описание", before?.desc, after.desc);
      change("Сумма", before && cash(before.amount_cents, group.currency), cash(after.amount_cents, group.currency));
      change("Заплатил(а)", before && name(before.payer), name(after.payer));
      const original = item => item?.orig_currency ? cash(item.orig_amount_cents, item.orig_currency) : "—";
      if (before) change("В исходной валюте", original(before), original(after));
      const ids = new Set([...Object.keys(before?.shares || {}), ...Object.keys(after.shares)]);
      for (const uid of ids) change(`Доля: ${name(uid)}`,
        before ? (uid in before.shares ? cash(before.shares[uid], group.currency) : "не участвовал(а)") : undefined,
        uid in after.shares ? cash(after.shares[uid], group.currency) : "не участвовал(а)");
      list.append(card);
    }
    offset += data.history.length;
    more.hidden = !data.more;
    if (!offset) list.append(el("p", "Изменений пока нет.", "hint"));
  }
  const more = button("Показать ещё", load, "secondary wide"); box.append(more);
  await load();
}
