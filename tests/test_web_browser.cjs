// Browser regressions using local source files and mocked API responses only.
// Run: node --test tests/test_web_browser.cjs (requires playwright).
const {before, after, test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');

let browser;
before(async () => { browser = await chromium.launch({headless: true, channel: process.env.MINIAPP_BROWSER_CHANNEL || undefined}); });
after(async () => { await browser?.close(); });

const group = {id: 1, title: 'Review fixture', currency: 'RUB', balance: 0,
  balances: {1: 0, 2: 0}, members: [{id: 1, name: 'Alice'}, {id: 2, name: 'Bob'}],
  transfers: [], is_owner: true, can_change_currency: false};

async function fixture(t) {
  const context = await browser.newContext({timezoneId: 'Europe/Moscow', viewport: {width: 390, height: 844}});
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  const requests = [];
  const activeGroup = structuredClone(group);
  const entries = Array.from({length: 31}, (_, i) => ({id: i + 1, desc: `Expense ${i}`,
    payer: 1, created_at: Date.UTC(2026, 8, 9, 12) / 1000, amount_cents: 100}));
  const detail = {...entries[0], created_by: 1, shares: {1: 80, 2: 20}, revision: 1,
    orig_currency: '', has_receipt: false, deleted: false, can_edit: true, can_restore: false};
  const initial = structuredClone(detail);
  await page.addInitScript(() => { window.Telegram = {WebApp: {initData: 'fixture',
    ready() {}, expand() {}, BackButton: {show() {}, hide() {}, onClick() {}}}}; });
  await page.route('https://telegram.org/**', route => route.fulfill({body: ''}));
  await page.route('https://miniapp.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.startsWith('/api/')) {
      requests.push({path: url.pathname, method: request.method()});
      let data = {};
      if (url.pathname.endsWith('/receipt')) {
        if (request.method() === 'POST') {
          assert.match(request.headers()['content-type'], /^image\//);
          assert.ok(request.postDataBuffer().length > 0);
          detail.has_receipt = true; detail.revision++;
          return route.fulfill({json: {ok: true}});
        }
        if (request.method() === 'DELETE') { detail.has_receipt = false; detail.revision++; return route.fulfill({json: {ok: true}}); }
        return route.fulfill({contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')});
      }
      if (url.pathname.endsWith('/export') && request.method() === 'GET') {
        return route.fulfill({body: Buffer.from('PK-test-workbook'), contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          headers: {'Content-Disposition': "attachment; filename*=UTF-8''group-1.xlsx"}});
      }
      if (url.pathname.endsWith('/history')) data = {history: [
        {id: 2, actor: 1, action: 'edit', created_at: detail.created_at, before: initial,
          after: {...initial, amount_cents: 200, shares: {1: 160, 2: 40}}},
      ], names: {1: 'Alice', 2: 'Bob'}, more: false};
      else if (/\/operations\//.test(url.pathname)) data = {expense: null};
      else if (url.pathname.endsWith('/restore')) {
        detail.deleted = false; detail.can_edit = true; detail.can_restore = false; data = {ok: true};
      }
      else if (url.pathname === '/api/groups/1/expenses/1' && request.method() === 'DELETE') {
        detail.deleted = true; detail.can_edit = false; detail.can_restore = true; data = {ok: true};
      }
      else if (url.pathname === '/api/groups/1/expenses/1' && request.method() === 'GET') data = detail;
      else if (request.method() === 'POST') {
        const body = request.postDataJSON(); writes.push(body); data = {id: 7, ok: true};
        if (url.pathname.endsWith('/settings')) Object.assign(activeGroup, body);
      }
      else if (url.pathname === '/api/me') data = {user: {id: 1}, currencies: ['RUB', 'USD', 'EUR']};
      else if (url.pathname === '/api/groups') data = {groups: [activeGroup]};
      else if (url.pathname === '/api/groups/1') data = activeGroup;
      else if (url.pathname === '/api/groups/1/expenses') {
        const offset = Number(url.searchParams.get('offset') || 0);
        const deleted = url.searchParams.get('deleted') === '1';
        const filtered = url.searchParams.get('payer') === '2' ? [] : entries.filter(entry => (entry.id === 1 && detail.deleted) === deleted);
        data = {expenses: filtered.slice(offset, offset + 30), total: filtered.length, sum_cents: filtered.length * 100};
      }
      return route.fulfill({json: data});
    }
    const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const contentType = relative.endsWith('.js') ? 'application/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html';
    await route.fulfill({body: fs.readFileSync(path.join(__dirname, '..', 'web', relative)), contentType});
  });
  await page.goto('https://miniapp.test/');
  await page.locator('.group').waitFor();
  await page.evaluate(group => {
    window.testGroup = group;
    window.testContext = {me: {id: 1}, currencies: ['RUB', 'USD', 'EUR'], reload: async () => {}};
  }, group);
  return {page, writes, requests, activeGroup};
}

async function form(page, existing = null) {
  await page.evaluate(async existing => {
    const {expenseForm} = await import('/expense.js');
    expenseForm(window.testGroup, existing, window.testContext);
  }, existing);
}

test('editing only the description preserves unequal shares in the saved request', async t => {
  const {page, writes} = await fixture(t);
  await form(page, {id: 7, desc: 'Unequal', payer: 1, amount_cents: 10000,
    shares: {1: 8000, 2: 2000}, revision: 3});
  await page.getByLabel('За что платили?').fill('Description changed');
  await page.getByRole('button', {name: 'Проверить трату'}).click();
  await page.getByRole('button', {name: 'Сохранить изменения'}).click();
  await page.locator('dialog').waitFor({state: 'hidden'});
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], {description: 'Description changed', amount_cents: 10000,
    payer: 1, participants: [1, 2], shares: [8000, 2000], revision: 3});
});

async function deferRates(page) {
  await page.evaluate(() => {
    const originalFetch = window.fetch;
    window.rateRequests = [];
    window.fetch = (url, options) => String(url).includes('/api/rate?')
      ? new Promise(resolve => window.rateRequests.push({url, resolve})) : originalFetch(url, options);
  });
}

async function resolveRate(page, currency, cents) {
  await page.evaluate(({currency, cents}) => {
    for (const request of window.rateRequests.filter(r => r.url.includes(`from=${currency}`))) {
      request.resolve(new Response(JSON.stringify({converted: cents, rate: cents / 1000})));
    }
  }, {currency, cents});
}

test('late FX responses cannot overwrite a newer currency or a manual amount', async t => {
  const {page} = await fixture(t);
  await deferRates(page);
  await form(page);
  await page.getByLabel('Сумма, RUB', {exact: true}).fill('10');
  await page.locator('dialog select').nth(0).selectOption('USD');
  await page.waitForFunction(() => window.rateRequests.some(r => r.url.includes('from=USD')));
  await page.locator('dialog select').nth(0).selectOption('EUR');
  await page.waitForFunction(() => window.rateRequests.some(r => r.url.includes('from=EUR')));
  await resolveRate(page, 'EUR', 100000);
  await page.waitForFunction(() => [...document.querySelectorAll('input')].some(n => n.value === '1000.00'));
  await resolveRate(page, 'USD', 90000);
  assert.equal(await page.getByLabel('Списано в валюте группы, RUB').inputValue(), '1000.00');
  await page.locator('dialog select').nth(0).selectOption('USD');
  await page.getByLabel('Списано в валюте группы, RUB').fill('777');
  await resolveRate(page, 'USD', 90000);
  assert.equal(await page.getByLabel('Списано в валюте группы, RUB').inputValue(), '777');
});

test('changing the amount invalidates its conversion before the debounce completes', async t => {
  const {page} = await fixture(t);
  await deferRates(page);
  await form(page);
  await page.getByLabel('За что платили?').fill('FX');
  await page.getByLabel('Сумма, RUB', {exact: true}).fill('10');
  await page.locator('dialog select').nth(0).selectOption('USD');
  await resolveRate(page, 'USD', 90000);
  await page.waitForFunction(() => [...document.querySelectorAll('input')].some(n => n.value === '900.00'));
  await page.getByLabel('Сумма, USD', {exact: true}).fill('20');
  assert.equal(await page.getByLabel('Списано в валюте группы, RUB').inputValue(), '');
  await page.getByRole('button', {name: 'Проверить трату'}).click();
  assert.equal(await page.getByRole('button', {name: 'Сохранить трату'}).count(), 0);
  await page.waitForFunction(() => window.rateRequests.some(r => r.url.includes('amount=2000')));
  await page.getByLabel('Сумма, USD', {exact: true}).fill('30');
  await resolveRate(page, 'USD', 180000);
  assert.equal(await page.getByLabel('Списано в валюте группы, RUB').inputValue(), '');
});

test('storage refusal keeps the offline form and its data available to retry', async t => {
  const {page, writes} = await fixture(t);
  await form(page);
  await page.getByLabel('За что платили?').fill('Keep this expense');
  await page.getByLabel('Сумма, RUB', {exact: true}).fill('100');
  await page.getByRole('button', {name: 'Проверить трату'}).click();
  await page.evaluate(() => {
    window.originalFetch = window.fetch;
    window.fetch = async () => { throw new TypeError('Offline'); };
    Storage.prototype.setItem = () => { throw new DOMException('Full', 'QuotaExceededError'); };
  });
  await page.getByRole('button', {name: 'Сохранить трату'}).click();
  assert.equal(await page.locator('dialog').evaluate(node => node.open), true);
  assert.equal(await page.getByLabel('За что платили?').inputValue(), 'Keep this expense');
  assert.match(await page.locator('#notice').innerText(), /Не удалось сохранить/);
  assert.equal(writes.length, 0);
  await page.evaluate(() => { window.fetch = window.originalFetch; });
  await page.getByRole('button', {name: 'Сохранить трату'}).click();
  await page.locator('dialog').waitFor({state: 'hidden'});
  assert.equal(writes.length, 1);
});

test('day subtotal grows across pages and resets when filters change', async t => {
  const {page} = await fixture(t);
  await page.locator('.group').click();
  await page.locator('.expense').first().waitFor();
  await page.getByRole('button', {name: 'Показать ещё'}).click();
  await page.locator('.expense').nth(30).waitFor();
  assert.equal(await page.locator('.expense').count(), 31);
  assert.equal(await page.locator('.day').count(), 1);
  assert.match(await page.locator('.day .amount').innerText(), /^31,00/);
  await page.getByLabel('Кто платил', {exact: true}).selectOption('2');
  await page.getByText('Ничего не нашлось.', {exact: false}).waitFor();
  assert.equal(await page.locator('.day').count(), 0);
  await page.getByLabel('Кто платил', {exact: true}).selectOption('0');
  await page.locator('.expense').first().waitFor();
  assert.match(await page.locator('.day .amount').innerText(), /^30,00/);
});

test('Moscow dates straddling midnight get separate matching day labels', async t => {
  const {page} = await fixture(t);
  const days = await page.evaluate(async () => {
    const {byDay, dayLabel} = await import('/lib/money.js');
    return byDay(['2026-09-09T22:00:00Z', '2026-09-09T20:00:00Z'].map(value => ({created_at: Date.parse(value) / 1000, amount_cents: 100})))
      .map(day => ({key: day.key, label: dayLabel(day.at, 'ru', Date.parse('2026-09-10T09:00:00Z')), total: day.total}));
  });
  assert.deepEqual(days, [{key: '2026-09-10', label: 'Сегодня', total: 100}, {key: '2026-09-09', label: 'Вчера', total: 100}]);
});

test('queue entries can be corrected, retried and explicitly removed', async t => {
  const {page, writes} = await fixture(t);
  await page.evaluate(async () => {
    const {enqueue} = await import('/lib/api.js');
    enqueue({path: '/groups/1/expenses', currency: 'RUB', group_title: 'Review fixture',
      body: {operation_id: 'browser-queue-operation', description: 'Queued coffee', amount_cents: 10000,
        payer: 1, participants: [1, 2], shares: [8000, 2000]}});
  });
  await page.getByRole('button', {name: 'Обновить'}).click();
  await page.getByRole('button', {name: 'Открыть очередь'}).click();
  await page.getByRole('button', {name: 'Изменить', exact: true}).click();
  await page.getByLabel('За что платили?').fill('Corrected coffee');
  await page.getByRole('button', {name: 'Проверить трату'}).click();
  await page.getByRole('button', {name: 'Сохранить изменения'}).click();
  await page.getByRole('heading', {name: 'Очередь отправки'}).waitFor();
  assert.match(await page.locator('dialog').innerText(), /Corrected coffee/);
  if (process.env.MINIAPP_SCREENSHOTS) await page.screenshot({path: path.join(process.env.MINIAPP_SCREENSHOTS, 'queue-new.png')});
  assert.equal(writes.length, 0);
  await page.getByRole('button', {name: 'Повторить', exact: true}).click();
  await page.getByText('Неотправленных трат нет.', {exact: true}).waitFor();
  assert.deepEqual(writes[0].shares, [8000, 2000]);
  assert.equal(writes[0].description, 'Corrected coffee');
  await page.evaluate(async () => {
    const {enqueue} = await import('/lib/api.js');
    enqueue({path: '/groups/1/expenses', body: {operation_id: 'remove-queue-operation', description: 'Remove me', amount_cents: 100, payer: 1, participants: [1]}});
    const {showQueue} = await import('/queue.js'); showQueue(window.testContext);
  });
  await page.getByRole('button', {name: 'Удалить', exact: true}).click();
  await page.getByRole('button', {name: 'Да', exact: true}).click();
  await page.getByText('Неотправленных трат нет.', {exact: true}).waitFor();
  assert.equal(writes.length, 1);
});

test('receipt upload, preview, history, deletion and restoration work in the expense card', async t => {
  const {page, requests} = await fixture(t);
  await page.locator('.group').click();
  await page.locator('.expense').first().click();
  await page.getByLabel('Фото чека').setInputFiles({name: 'receipt.jpg', mimeType: 'image/jpeg', buffer: Buffer.from([255, 216, 255, 1])});
  await page.getByRole('button', {name: 'Прикрепить чек', exact: true}).click();
  await page.getByRole('button', {name: 'Посмотреть чек'}).click();
  await page.locator('img.receipt-image').waitFor();
  if (process.env.MINIAPP_SCREENSHOTS) await page.screenshot({path: path.join(process.env.MINIAPP_SCREENSHOTS, 'receipt-new.png')});
  assert.ok(requests.some(r => r.path.endsWith('/receipt') && r.method === 'POST'));
  assert.ok(requests.some(r => r.path.endsWith('/receipt') && r.method === 'GET'));
  await page.getByRole('button', {name: 'История изменений'}).click();
  await page.locator('.history-event').waitFor();
  const history = await page.locator('.history-event').innerText();
  if (process.env.MINIAPP_SCREENSHOTS) await page.screenshot({path: path.join(process.env.MINIAPP_SCREENSHOTS, 'history-new.png')});
  assert.match(history, /Alice/); assert.match(history, /1,00.*2,00/); assert.match(history, /Доля: Bob/);
  await page.getByRole('button', {name: '‹ К трате'}).click();
  await page.getByRole('button', {name: 'Удалить трату', exact: true}).click();
  await page.getByRole('button', {name: 'Да', exact: true}).click();
  await page.getByRole('heading', {name: 'Трата удалена'}).waitFor();
  await page.getByRole('button', {name: 'Закрыть', exact: true}).click();
  await page.getByLabel('Статус трат').selectOption('1');
  await page.locator('.expense').first().click();
  await page.getByRole('button', {name: 'Восстановить трату'}).click();
  await page.getByRole('button', {name: 'Изменить', exact: true}).waitFor();
  assert.ok(requests.some(r => r.path.endsWith('/restore') && r.method === 'POST'));
});

test('group editing and Excel download or Telegram delivery are available inside the app', async t => {
  const {page, writes, requests, activeGroup} = await fixture(t);
  activeGroup.can_change_currency = true;
  await page.locator('.group').click();
  await page.getByRole('button', {name: 'Редактировать группу', exact: true}).click();
  await page.getByLabel('Название', {exact: true}).fill('Renamed group');
  await page.getByLabel('Валюта группы').selectOption('EUR');
  await page.getByRole('button', {name: 'Сохранить', exact: true}).click();
  await page.getByRole('heading', {name: 'Renamed group', exact: true}).waitFor();
  assert.deepEqual(writes[0], {title: 'Renamed group', currency: 'EUR'});
  await page.getByRole('button', {name: 'Экспорт Excel'}).click();
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', {name: 'Скачать Excel'}).click()]);
  assert.equal(download.suggestedFilename(), 'group-1.xlsx');
  await page.getByRole('button', {name: 'Получить в Telegram'}).click();
  assert.ok(requests.some(r => r.path.endsWith('/export') && r.method === 'POST'));
  assert.match(await page.locator('#notice').innerText(), /Excel отправлен/);
});

test('an HTML upload rejection explains the size limit and preserves the selected receipt', async t => {
  const {page} = await fixture(t);
  await page.route('**/api/groups/1/expenses/1/receipt?*', route => route.fulfill({
    status: 413, contentType: 'text/html', body: '<html><h1>413 Request Entity Too Large</h1></html>',
  }));
  await page.locator('.group').click();
  await page.locator('.expense').first().click();
  await page.getByLabel('Фото чека').setInputFiles({name: 'receipt.png', mimeType: 'image/png', buffer: Buffer.alloc(40000)});
  await page.getByRole('button', {name: 'Прикрепить чек', exact: true}).click();
  const message = await page.locator('#notice').innerText();
  assert.match(message, /фото.*лимит/);
  assert.match(message, /HTTP 413/);
  assert.equal(await page.getByLabel('Фото чека').evaluate(node => node.files[0].name), 'receipt.png');
  assert.equal(await page.locator('dialog').evaluate(node => node.open), true);
});
