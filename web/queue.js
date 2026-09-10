import {api, pending, legacyPending, importLegacyQueue, flush, queueBusy, removeQueued, updateQueued} from "./lib/api.js";
import {el, button, row, openModal, ask, notify} from "./lib/dom.js";
import {money, equalShares} from "./lib/money.js";
import {locale} from "./lib/tg.js";
import {expenseForm, showExpense} from "./expense.js";

export function showQueue(ctx) {
  const box = openModal("Очередь отправки");
  const redraw = () => showQueue(ctx);
  const entries = pending();
  box.append(el("p", "Эти траты сохранены на этом устройстве для вашего аккаунта. Ошибки не удаляют записи.", "hint"));
  if (queueBusy()) box.append(el("p", "Идёт отправка. Дождитесь её завершения перед изменением записей.", "hint"));
  if (legacyPending().length) {
    box.append(button("Перенести старую очередь", () => ask(
      "На устройстве есть очередь из предыдущей версии без привязки к аккаунту. Перенести её в ваш аккаунт? Подтверждайте, только если это ваши траты.",
      async () => { importLegacyQueue(); redraw(); },
    ), "secondary wide"));
  }
  if (!entries.length) box.append(el("p", "Неотправленных трат нет.", "empty"));
  for (const entry of entries) {
    const id = entry.body.operation_id;
    const card = el("div", undefined, "card");
    const gid = Number(entry.path.match(/\/groups\/(\d+)\//)?.[1]);
    const sum = entry.currency ? money(entry.body.amount_cents, entry.currency, locale)
      : `${(entry.body.amount_cents / 100).toFixed(2)} (валюта группы)`;
    card.append(row(entry.body.description, sum),
      el("p", entry.group_title || `Группа ${gid}`, "muted"),
      el("p", entry.status === "failed" ? "Требует исправления" : "Ожидает отправки", "queue-status"));
    if (entry.error) card.append(el("p", entry.error, "hint"));
    const actions = el("div", undefined, "actions");
    actions.append(button("Повторить", async () => {
      const result = await flush(id);
      if (result.sent) await ctx.reload();
      redraw();
      notify(result.error || "Трата отправлена");
    }), button("Изменить", async () => {
      if (queueBusy()) throw new Error("Дождитесь завершения отправки.");
      // Resolve a write whose response may have been lost before editing it.
      const group = await api(`/groups/${gid}`);
      const {expense} = await api(`/groups/${gid}/operations/${id}`);
      if (expense?.deleted) {
        await showExpense(group, expense.id, ctx);
        notify("Эта трата уже была отправлена и удалена. Её можно восстановить или убрать запись из очереди.");
        return;
      }
      const body = entry.body;
      const shares = body.shares || equalShares(body.amount_cents, body.participants.length);
      const draft = {...expense, desc: body.description, amount_cents: body.amount_cents,
        payer: body.payer, orig_currency: body.orig_currency || "", orig_amount_cents: body.orig_amount_cents || 0,
        shares: Object.fromEntries(body.participants.map((uid, index) => [uid, shares[index]]))};
      expenseForm(group, draft, {...ctx,
        ...(expense ? {afterSave: () => removeQueued(id)} : {saveQueued: payload => updateQueued(id,
          {...payload, group_currency: group.currency}, {currency: group.currency, group_title: group.title})}),
        reload: async () => { await ctx.reload(); redraw(); },
      });
      if (entry.currency && entry.currency !== group.currency) notify(`Валюта группы изменилась с ${entry.currency} на ${group.currency}. Проверьте сумму перед сохранением.`);
    }, "secondary"), button("Удалить", () => ask(
      "Удалить запись с устройства? Если она уже успела попасть в группу, сама трата останется в группе.",
      async () => { removeQueued(id); redraw(); },
    ), "danger"));
    card.append(actions);
    box.append(card);
  }
  if (entries.length) box.append(button("Отправить ожидающие", async () => {
    const result = await flush();
    if (result.sent) await ctx.reload();
    redraw();
    if (result.error) notify(result.error);
  }, "wide"));
}
