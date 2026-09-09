// Talking to the server: one request shape, a memory cache that lets a screen
// paint before the network answers, and a queue for writes that lost the
// connection.
"use strict";

import {tg} from "./tg.js";

const NO_NETWORK = "Нет связи с сервером. Проверьте интернет и повторите.";
const cache = new Map();
const QUEUE_KEY = "expensebot.pending";

export async function api(path, method = "GET", body, signal) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: {
        "Authorization": `tma ${tg?.initData || ""}`,
        ...(body === undefined ? {} : {"Content-Type": "application/json"}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
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
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(data.error || "Сервер недоступен. Попробуйте ещё раз.");
    failure.status = response.status;
    throw failure;
  }
  return data;
}

export const cached = path => cache.get(path);
export const remember = (path, data) => cache.set(path, data);
// Anything a write may have changed: after saving an expense the group card,
// its list and every debt figure are all suspect.
export const forget = () => cache.clear();

// -- offline queue ---------------------------------------------------------
// Every write the app makes carries an operation_id the server treats as
// idempotent, so replaying one that may already have landed is safe.
const readQueue = () => {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch { return []; }
};
const writeQueue = items => {
  try { localStorage.setItem(QUEUE_KEY, JSON.stringify(items)); } catch { /* private mode */ }
};

export const pending = () => readQueue();

export function enqueue(entry) {
  const items = readQueue();
  if (items.some(item => item.body.operation_id === entry.body.operation_id)) return;
  items.push(entry);
  writeQueue(items);
}

/** Replays queued writes oldest first: {sent} landed, {dropped} were refused. */
export async function flush() {
  let items = readQueue();
  let sent = 0, dropped = 0;
  while (items.length) {
    const [entry] = items;
    try {
      await api(entry.path, "POST", entry.body);
      sent++;
    } catch (error) {
      // Still offline: leave the queue for the next attempt. A refusal from
      // the server itself means this write will never succeed — drop it.
      if (error.offline) break;
      dropped++;
    }
    items = readQueue().filter(item => item.body.operation_id !== entry.body.operation_id);
    writeQueue(items);
  }
  if (sent || dropped) forget();
  return {sent, dropped};
}
