// Money arithmetic and offline queue. Run with:
//   node --test tests/test_web.js
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
  const at = (day, hour) => Math.floor(new Date(2026, 8, day, hour).getTime() / 1000);
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

test("day grouping uses the same local midnight as its labels", () => {
  const now = new Date(2026, 8, 10, 12).getTime();
  const days = byDay([10, 9].map(day => ({
    created_at: new Date(2026, 8, day, day === 10 ? 1 : 23).getTime() / 1000,
    amount_cents: 100,
  })));
  assert.deepEqual(days.map(day => day.key), ["2026-09-10", "2026-09-09"]);
  assert.deepEqual(days.map(day => dayLabel(day.at, "ru", now)), ["Сегодня", "Вчера"]);
});

test("offline queue survives retryable errors and reports storage failures", async t => {
  const previous = {window: globalThis.window, localStorage: globalThis.localStorage, fetch: globalThis.fetch};
  const storage = new Map();
  globalThis.window = {};
  globalThis.localStorage = {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)};
  try {
    const {api, enqueue, pending, flush, setQueueUser, updateQueued, removeQueued, legacyPending, importLegacyQueue} = await import("../web/lib/api.js");
    setQueueUser(1);
    const entry = {path: "/groups/1/expenses", body: {operation_id: "test-operation-123456", amount_cents: 100}};
    await t.test("HTML proxy failures preserve the status and explain rejected uploads", async () => {
      for (const status of [413, 429, 502, 504]) {
        globalThis.fetch = async () => new Response('<html>proxy error</html>', {status, headers: {'Content-Type': 'text/html'}});
        await assert.rejects(api('/groups/1/expenses/1/receipt?revision=1', 'POST', new Blob(['photo'], {type: 'image/png'})), error => {
          assert.equal(error.status, status);
          assert.match(error.message, new RegExp(`HTTP ${status}`));
          if (status === 413) assert.match(error.message, /фото.*лимит/);
          return true;
        });
      }
      globalThis.fetch = async () => new Response(JSON.stringify({error: 'Specific server error'}), {status: 413});
      await assert.rejects(api('/groups/1/expenses/1/receipt'), /Specific server error/);
    });
    for (const status of [401, 408, 429, 500, 502, 503]) {
      await t.test(`HTTP ${status} preserves writes for a later successful retry`, async () => {
        storage.clear();
        enqueue(entry);
        globalThis.fetch = async () => new Response("{}", {status});
        const result = await flush();
        assert.equal(result.failed, 1);
        assert.ok(result.error);
        assert.deepEqual(pending().map(item => item.body), [entry.body]);
        globalThis.fetch = async (_url, options) => {
          assert.deepEqual(JSON.parse(options.body), entry.body);
          return new Response('{"id":7}');
        };
        assert.equal((await flush()).sent, 1);
        assert.deepEqual(pending(), []);
      });
    }
    await t.test("network loss preserves writes, permanent rejection does not block the next write", async () => {
      storage.clear();
      enqueue(entry);
      globalThis.fetch = async () => { throw new TypeError("Network failed"); };
      assert.equal((await flush()).failed, 1);
      assert.deepEqual(pending().map(item => item.body), [entry.body]);
      enqueue({...entry, body: {...entry.body, operation_id: "test-operation-654321"}});
      let calls = 0;
      globalThis.fetch = async () => new Response("{}", {status: ++calls === 1 ? 400 : 200});
      const result = await flush();
      assert.equal(result.sent, 1);
      assert.equal(result.failed, 1);
      assert.equal(pending().length, 1);
      assert.equal(pending()[0].status, "failed");
      assert.ok(pending()[0].error);
      updateQueued(entry.body.operation_id, {...entry.body, amount_cents: 200});
      globalThis.fetch = async () => new Response('{"id":7}');
      assert.equal((await flush()).sent, 1);
      assert.deepEqual(pending(), []);
    });
    await t.test("accounts are isolated and legacy entries require explicit import", async () => {
      storage.clear();
      setQueueUser(1); enqueue(entry);
      setQueueUser(2);
      assert.deepEqual(pending(), []);
      let calls = 0;
      globalThis.fetch = async () => { calls++; return new Response('{"id":7}'); };
      await flush(); assert.equal(calls, 0);
      storage.set("expensebot.pending", JSON.stringify([entry]));
      assert.equal(legacyPending().length, 1);
      await flush(); assert.equal(calls, 0);
      importLegacyQueue(); assert.equal(pending().length, 1);
      assert.equal(legacyPending().length, 0);
      removeQueued(entry.body.operation_id); assert.equal(pending().length, 0);
      setQueueUser(1); assert.equal(pending().length, 1);
    });
    await t.test("simultaneous flushes share one request and block edits while sending", async () => {
      storage.clear(); enqueue(entry);
      let finish, calls = 0;
      globalThis.fetch = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
      const first = flush(), second = flush();
      assert.equal(first, second);
      assert.throws(() => updateQueued(entry.body.operation_id, entry.body), /Дождитесь/);
      assert.throws(() => removeQueued(entry.body.operation_id), /Дождитесь/);
      finish(new Response('{"id":7}'));
      await first;
      assert.equal(calls, 1);
      assert.equal(pending().length, 0);
    });
    await t.test("storage failure is reported and never erases an existing queue", () => {
      storage.clear();
      enqueue(entry);
      globalThis.localStorage.setItem = () => { throw new Error("Quota exceeded"); };
      assert.throws(() => enqueue({...entry, body: {...entry.body, operation_id: "another-operation-123"}}), /Не удалось сохранить/);
      assert.deepEqual(pending().map(item => item.body), [entry.body]);
    });
    await t.test("disabled storage does not break online screens or pretend to save", () => {
      globalThis.localStorage.getItem = () => { throw new Error("Storage disabled"); };
      assert.deepEqual(pending(), []);
      assert.deepEqual(legacyPending(), []);
      assert.throws(() => enqueue(entry), /Не удалось прочитать/);
      assert.throws(() => importLegacyQueue(), /Не удалось прочитать/);
    });
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});
