// Money arithmetic the Mini App does on its own. Run with:
//   node --test tests/
import test from "node:test";
import assert from "node:assert/strict";

import {amount, money, equalShares, partShares, groupIcon, byDay, dayLabel} from "../web/lib/money.js";

test("amount accepts what people actually type", () => {
  assert.equal(amount("10"), 1000);
  assert.equal(amount("1200,50"), 120050);
  assert.equal(amount("1 200,50"), 120050);       // ordinary space
  assert.equal(amount("1 200,50"), 120050);      // non-breaking, as printed
  assert.equal(amount("1 200.5"), 120050);       // narrow, from copied output
  assert.equal(amount("0", true), 0);
});

test("amount refuses what would silently lose money", () => {
  for (const bad of ["", "abc", "-5", "1.234", "1,2,3", "1 000 000 000,01", "0"]) {
    assert.throws(() => amount(bad), Error, `accepted ${bad}`);
  }
  assert.throws(() => amount("0"), /Проверьте сумму/);
});

test("equal split spends every kopeck, remainder first", () => {
  assert.deepEqual(equalShares(1000, 3), [334, 333, 333]);
  assert.deepEqual(equalShares(10001, 2), [5001, 5000]);
  for (const [cents, count] of [[1, 3], [100, 7], [99999, 13]]) {
    assert.equal(equalShares(cents, count).reduce((a, b) => a + b, 0), cents);
  }
});

test("parts split weights, not amounts", () => {
  assert.deepEqual(partShares(40000, [2, 1, 1]), [20000, 10000, 10000]);
  assert.deepEqual(partShares(1000, [1, 1, 1]), [334, 333, 333]);
  // The leftover goes to the largest weight, so the biggest share absorbs it.
  assert.deepEqual(partShares(100, [3, 1]), [75, 25]);
  assert.deepEqual(partShares(10, [1, 1, 1]), [4, 3, 3]);
  assert.throws(() => partShares(1000, [0, 0]), /долю больше нуля/);
});

test("parts split never loses or invents a kopeck", () => {
  const cases = [[9999, [1, 1, 1]], [1, [5, 3, 2]], [123456, [7, 11, 13]], [100, [1]]];
  for (const [cents, weights] of cases) {
    const shares = partShares(cents, weights);
    assert.equal(shares.reduce((a, b) => a + b, 0), cents);
    assert.ok(shares.every(share => share >= 0));
  }
});

test("money falls back when the currency code is malformed", () => {
  assert.match(money(215002, "RUB", "ru"), /2\s?150,02/);
  // Intl throws on a code that is not three letters; the number still has to
  // reach the screen with its currency beside it.
  assert.match(money(100, "XX", "ru"), /^1,00\sXX$/);
});

test("group icon follows the title, then the id", () => {
  assert.equal(groupIcon({id: 1, title: "Поездка в Стамбул"}), "✈️");
  assert.equal(groupIcon({id: 2, title: "Квартира"}), "🏠");
  // Nothing recognisable: stable per id, not random.
  assert.equal(groupIcon({id: 3, title: "Zzz"}), groupIcon({id: 3, title: "Qqq"}));
});

test("byDay keeps the server's order and totals each day", () => {
  const at = (day, hour) => Math.floor(Date.UTC(2026, 8, day, hour) / 1000);
  const days = byDay([
    {created_at: at(9, 20), amount_cents: 300},
    {created_at: at(9, 2), amount_cents: 200},
    {created_at: at(8, 23), amount_cents: 100},
  ]);
  assert.deepEqual(days.map(day => day.expenses.length), [2, 1]);
  assert.deepEqual(days.map(day => day.total), [500, 100]);
  assert.equal(byDay([]).length, 0);
});

test("dayLabel names the recent days", () => {
  const now = Date.UTC(2026, 8, 9, 12);
  const seconds = offsetDays => Math.floor(now / 1000) - offsetDays * 86400;
  assert.equal(dayLabel(seconds(0), "ru", now), "Сегодня");
  assert.equal(dayLabel(seconds(1), "ru", now), "Вчера");
  assert.match(dayLabel(seconds(30), "ru", now), /август|августа|авг/);
});
