// Money and splitting. Nothing here touches the DOM or the network, so it is
// the part that can be tested outright — see tests/test_web.js.
"use strict";

export const MAX_CENTS = 1000000000;

// The digits a person types are not the digits a program prints: thin and
// non-breaking spaces come back from copied output, and a comma is the
// decimal separator here.
export function amount(value, allowZero = false) {
  const text = String(value ?? "").replace(/[\s   '’]/g, "").replace(",", ".");
  if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error("Введите сумму с точностью до копеек.");
  const [whole, fraction = ""] = text.split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(cents) || cents > MAX_CENTS || cents < (allowZero ? 0 : 1)) {
    throw new Error("Проверьте сумму.");
  }
  return cents;
}

export function money(cents, currency, locale = "ru") {
  const value = cents / 100;
  const digits = {minimumFractionDigits: 2, maximumFractionDigits: 2};
  try {
    return new Intl.NumberFormat(locale, {style: "currency", currency, ...digits}).format(value);
  } catch {
    // An unknown code is still worth showing next to the number.
    return `${value.toLocaleString(locale, digits)} ${currency}`;
  }
}

// The remainder goes to the first people in the list, matching what the
// server does when it splits an amount itself.
export const equalShares = (cents, count) =>
  Array.from({length: count}, (_, index) => Math.floor(cents / count) + (index < cents % count ? 1 : 0));

// Weights, not amounts: 2:1:1 of 400 is 200/100/100. Leftover kopecks go to
// the largest weights first, so the biggest share absorbs the rounding.
export function partShares(cents, parts) {
  const total = parts.reduce((sum, value) => sum + value, 0);
  if (total <= 0) throw new Error("Укажите хотя бы одну долю больше нуля.");
  const shares = parts.map(part => Math.floor((cents * part) / total));
  let rest = cents - shares.reduce((sum, value) => sum + value, 0);
  const order = parts.map((_, index) => index).sort((a, b) => parts[b] - parts[a] || a - b);
  for (let index = 0; rest > 0; index++, rest--) shares[order[index % order.length]]++;
  return shares;
}

const ICONS = ["✈️", "🏠", "🍽️", "🎁", "🛒", "🎬", "🚗", "🏖️", "☕", "🎉"];
const ICON_WORDS = [
  [/поезд|путеш|отпуск|тур|trip|travel/i, "✈️"],
  [/кварт|дом|дач|ремонт|house|flat/i, "🏠"],
  [/еда|ужин|обед|рестор|кафе|бар|food/i, "🍽️"],
  [/подар|празд|рожден|gift/i, "🎁"],
  [/магаз|покуп|продукт|shop/i, "🛒"],
  [/море|пляж|beach|курорт/i, "🏖️"],
  [/маш|авто|такси|car/i, "🚗"],
  [/офис|работ|проект|команд|team/i, "💼"],
];
// A group needs a face of its own: the title picks it when it can, and the id
// keeps the fallback stable across screens.
export const groupIcon = group =>
  (ICON_WORDS.find(([pattern]) => pattern.test(group.title)) || [])[1] || ICONS[group.id % ICONS.length];

// Expenses arrive newest first; keep that order and cut them into days.
export function byDay(expenses) {
  const days = [];
  for (const expense of expenses) {
    // Use the same local calendar as dayLabel, including DST transitions.
    const date = new Date(expense.created_at * 1000);
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    const last = days[days.length - 1];
    if (last && last.key === key) last.expenses.push(expense);
    else days.push({key, at: expense.created_at, expenses: [expense]});
  }
  return days.map(day => ({
    ...day,
    total: day.expenses.reduce((sum, expense) => sum + expense.amount_cents, 0),
  }));
}

export function dayLabel(seconds, locale = "ru", now = Date.now()) {
  const date = new Date(seconds * 1000);
  const days = Math.round((new Date(now).setHours(0, 0, 0, 0) - new Date(date).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return "Сегодня";
  if (days === 1) return "Вчера";
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString(locale, {day: "numeric", month: "long", ...(sameYear ? {} : {year: "numeric"})});
}
