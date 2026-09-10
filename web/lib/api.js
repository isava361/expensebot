// Talking to the server: one request shape, a memory cache that lets a screen
// paint before the network answers, and a queue for writes that lost the
// connection.
"use strict";

import {tg} from "./tg.js";

const NO_NETWORK = "Нет связи с сервером. Проверьте интернет и повторите.";
const cache = new Map();
const QUEUE_KEY = "expensebot.pending";
let queueUser = null, flushing = null;

async function request(path, method = "GET", body, signal) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: {
        "Authorization": `tma ${tg?.initData || ""}`,
        ...(body === undefined ? {} : {"Content-Type": body instanceof Blob ? body.type : "application/json"}),
      },
      body: body === undefined || body instanceof Blob ? body : JSON.stringify(body),
      cache: "no-store",
      signal,
    });
  } catch (error) {
    // An abort is the caller's own doing; a dropped connection rejects with
    // the browser's own English text.
    if (error.name === "AbortError") throw error;
    const offline = new Error(NO_NETWORK);
    offline.offline = true;
    throw offline;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    // Reverse proxies often return HTML, before the request reaches our API.
    const receipt = path.split("?")[0].endsWith("/receipt");
    const messages = {
      401: "Откройте приложение заново из меню бота в Telegram.",
      403: "Нет доступа к этому действию.",
      413: receipt ? "Сервер отклонил фото: превышен лимит загрузки (HTTP 413)."
        : "Сервер отклонил слишком большой запрос (HTTP 413).",
      429: "Слишком много запросов. Подождите немного и повторите (HTTP 429).",
      502: "Сервер временно недоступен (HTTP 502). Попробуйте ещё раз.",
      504: "Сервер не успел ответить (HTTP 504). Попробуйте ещё раз.",
    };
    const failure = new Error((typeof data?.error === "string" && data.error)
      || messages[response.status] || `Ошибка сервера (HTTP ${response.status}). Попробуйте ещё раз.`);
    failure.status = response.status;
    throw failure;
  }
  return response;
}

export async function api(path, method = "GET", body, signal) {
  return (await request(path, method, body, signal)).json();
}

export async function apiFile(path) {
  const response = await request(path);
  const encoded = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  return {blob: await response.blob(), filename: encoded ? decodeURIComponent(encoded) : "expenses.xlsx"};
}

export const cached = path => cache.get(path);
export const remember = (path, data) => cache.set(path, data);
// Anything a write may have changed: after saving an expense the group card,
// its list and every debt figure are all suspect.
export const forget = () => cache.clear();

// -- offline queue ---------------------------------------------------------
// Every write the app makes carries an operation_id the server treats as
// idempotent, so replaying one that may already have landed is safe.
export function setQueueUser(uid) {
  if (!Number.isSafeInteger(uid) || uid <= 0) throw new Error("Не удалось определить владельца очереди.");
  queueUser = uid;
}
const queueKey = () => queueUser === null ? null : `${QUEUE_KEY}.${queueUser}`;
const readQueue = (key = queueKey()) => {
  if (!key) return [];
  try {
    const items = JSON.parse(localStorage.getItem(key) || "[]");
    if (!Array.isArray(items)) throw new Error();
    return items;
  } catch { throw new Error("Не удалось прочитать очередь на устройстве. Проверьте доступ к хранилищу."); }
};
const writeQueue = (items, key = queueKey()) => {
  if (!key) throw new Error("Откройте приложение заново из Telegram.");
  try { localStorage.setItem(key, JSON.stringify(items)); }
  catch { throw new Error("Не удалось сохранить трату на устройстве. Не закрывайте форму: восстановите связь и повторите отправку."); }
};

// Browsing online still works when WebView storage is disabled. Mutations use
// strict reads, so they cannot overwrite unreadable entries or claim a save.
const availableQueue = key => { try { return readQueue(key); } catch { return []; } };
export const pending = () => availableQueue(queueKey());
export const legacyPending = () => availableQueue(QUEUE_KEY);
export const queueBusy = () => Boolean(flushing);
const editableQueue = () => { if (flushing) throw new Error("Дождитесь завершения отправки."); };

export function importLegacyQueue() {
  editableQueue();
  const items = readQueue();
  for (const entry of readQueue(QUEUE_KEY)) {
    if (!items.some(item => item.body.operation_id === entry.body.operation_id)) items.push(entry);
  }
  writeQueue(items);
  writeQueue([], QUEUE_KEY);
}

export function enqueue(entry) {
  const items = readQueue();
  if (items.some(item => item.body.operation_id === entry.body.operation_id)) return;
  items.push({...entry, created_at: Date.now(), status: "waiting", error: ""});
  writeQueue(items);
}

export function removeQueued(operation) {
  editableQueue();
  writeQueue(readQueue().filter(item => item.body.operation_id !== operation));
}

export function updateQueued(operation, body, metadata = {}) {
  editableQueue();
  const items = readQueue();
  const item = items.find(entry => entry.body.operation_id === operation);
  if (!item) throw new Error("Трата уже отправлена или удалена из очереди.");
  item.body = {...body, operation_id: operation};
  item.status = "waiting";
  item.error = "";
  if (metadata.currency) item.currency = metadata.currency;
  if (metadata.group_title) item.group_title = metadata.group_title;
  writeQueue(items);
}

/** Replays queued writes oldest first; retryable failures leave the queue intact. */
export function flush(operation = null) {
  if (flushing) return flushing;
  flushing = flushQueue(operation).finally(() => { flushing = null; });
  return flushing;
}

async function flushQueue(operation) {
  const key = queueKey();
  let sent = 0, failed = 0, errorMessage = "";
  const entries = readQueue(key).filter(entry => operation ? entry.body.operation_id === operation : entry.status !== "failed");
  for (const entry of entries) {
    if (queueKey() !== key) break;
    try {
      await api(entry.path, "POST", entry.body);
      sent++;
      writeQueue(readQueue(key).filter(item => item.body.operation_id !== entry.body.operation_id), key);
      forget();
    } catch (error) {
      failed++;
      errorMessage = error.message;
      const permanent = error.status >= 400 && error.status < 500 && ![401, 408, 429].includes(error.status);
      writeQueue(readQueue(key).map(item => item.body.operation_id === entry.body.operation_id
        ? {...item, status: permanent ? "failed" : "waiting", error: error.message} : item), key);
      if (!permanent) break;
    }
  }
  return {sent, failed, error: errorMessage};
}
