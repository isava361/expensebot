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
  const context = await browser.newContext({timezoneId: 'Europe/Moscow'});
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const writes = [];
  const entries = Array.from({length: 31}, (_, i) => ({id: i + 1, desc: `Expense ${i}`,
    payer: 1, created_at: Date.UTC(2026, 8, 9, 12) / 1000, amount_cents: 100}));
  await page.addInitScript(() => { window.Telegram = {WebApp: {initData: 'fixture',
    ready() {}, expand() {}, BackButton: {show() {}, hide() {}, onClick() {}}}}; });
  await page.route('https://telegram.org/**', route => route.fulfill({body: ''}));
  await page.route('https://miniapp.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.startsWith('/api/')) {
      let data = {};
      if (request.method() === 'POST') { writes.push(request.postDataJSON()); data = {id: 7, ok: true}; }
      else if (url.pathname === '/api/me') data = {user: {id: 1}, currencies: ['RUB', 'USD', 'EUR']};
      else if (url.pathname === '/api/groups') data = {groups: [group]};
      else if (url.pathname === '/api/groups/1') data = group;
      else if (url.pathname === '/api/groups/1/expenses') {
        const offset = Number(url.searchParams.get('offset') || 0);
        const filtered = url.searchParams.get('payer') === '2' ? [] : entries;
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
  return {page, writes};
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
  assert.equal(await page.locator('.expense').count(), 31);
  assert.equal(await page.locator('.day').count(), 1);
  assert.match(await page.locator('.day .amount').innerText(), /^31,00/);
  await page.locator('.filters select').selectOption('2');
  await page.getByText('Ничего не нашлось.', {exact: false}).waitFor();
  assert.equal(await page.locator('.day').count(), 0);
  await page.locator('.filters select').selectOption('0');
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
