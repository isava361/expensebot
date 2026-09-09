// Everything that only makes sense inside Telegram: its buttons, its haptics,
// its theme, and the viewport gymnastics an on-screen keyboard forces.
"use strict";

export const tg = window.Telegram?.WebApp;
export const locale = (tg?.initDataUnsafe?.user?.language_code || "ru").slice(0, 5);
// Older clients throw on methods they do not know; showConfirm lands in 6.2.
export const supports = version => Boolean(tg?.isVersionAtLeast?.(version));

export const haptic = kind => {
  const feedback = tg?.HapticFeedback;
  if (!feedback) return;
  try {
    ["success", "error", "warning"].includes(kind)
      ? feedback.notificationOccurred(kind)
      : feedback.impactOccurred(kind || "light");
  } catch { /* older clients */ }
};

// -- main button -----------------------------------------------------------
// The primary action of every screen and dialog lives in Telegram's own
// button: it stays pinned above the keyboard and never scrolls away. Without a
// Telegram host (a plain browser) the in-page button is shown instead.
let mainHandler = null, screenMain = null, onError = () => {};

export const setMainErrorHandler = handler => { onError = handler; };

export function bindMain(text, action) {
  if (!tg?.MainButton) return false;
  if (mainHandler) tg.MainButton.offClick(mainHandler);
  mainHandler = async () => {
    if (tg.MainButton.isProgressVisible) return;
    haptic("light");
    tg.MainButton.showProgress(true);
    try { await action(); } catch (error) { haptic("error"); onError(error); } finally { tg.MainButton.hideProgress(); }
  };
  tg.MainButton.setParams({text, is_visible: true, is_active: true});
  tg.MainButton.onClick(mainHandler);
  return true;
}

export function unbindMain() {
  if (mainHandler && tg?.MainButton) { tg.MainButton.offClick(mainHandler); tg.MainButton.hide(); }
  mainHandler = null;
}

export function setScreenMain(text, action, dialogOpen) {
  screenMain = text ? {text, action} : null;
  if (!dialogOpen) restoreMain();
}
export function restoreMain() {
  if (screenMain) bindMain(screenMain.text, screenMain.action); else unbindMain();
}

export const backState = visible => { if (visible) tg?.BackButton.show(); else tg?.BackButton.hide(); };

// -- viewport --------------------------------------------------------------
// The keyboard shrinks the visual viewport but not the layout one, so a dialog
// sized in vh ends up half-covered. Publish the visible band as CSS variables
// and let the dialog sit inside it.
export function resizeViewport() {
  const view = window.visualViewport;
  // A stable Telegram height lags behind keyboard transitions; prefer the
  // browser's own measurement when it is available.
  const height = view?.height || tg?.viewportStableHeight || innerHeight;
  const top = view?.offsetTop || 0;
  const root = document.documentElement;
  root.style.setProperty("--visible-height", `${height}px`);
  root.style.setProperty("--visible-top", `${top}px`);
  root.style.setProperty("--keyboard-bottom", `${Math.max(0, root.clientHeight - top - height)}px`);
}

// overflow:hidden alone does not stop iOS panning the page behind a dialog to
// reveal the focused field; the body has to be pinned where it stood.
let lockedPage = null;
export const isLocked = () => Boolean(lockedPage);
export function setPageLocked(locked) {
  const root = document.documentElement;
  if (locked && !lockedPage) {
    lockedPage = {x: window.scrollX, y: window.scrollY};
    root.style.setProperty("--locked-width", `${document.body.getBoundingClientRect().width}px`);
    root.style.setProperty("--locked-top", `${-lockedPage.y}px`);
    root.style.setProperty("--locked-left", `${-lockedPage.x}px`);
    root.classList.add("locked");
  } else if (!locked && lockedPage) {
    const {x, y} = lockedPage;
    lockedPage = null;
    root.classList.remove("locked");
    ["--locked-width", "--locked-top", "--locked-left"].forEach(name => root.style.removeProperty(name));
    window.scrollTo({left: x, top: y, behavior: "instant"});
  }
}

// iOS pans its visual viewport with a finger even through overflow:hidden once
// the keyboard is up. While the page is locked, a drag may only scroll
// something inside the dialog that still has room to move.
export function guardTouches(dialog) {
  let touchState = null;
  document.addEventListener("touchstart", event => {
    touchState = null;
    if (!lockedPage || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const box = dialog.open ? dialog.getBoundingClientRect() : null;
    const inside = box && touch.clientX >= box.left && touch.clientX <= box.right
      && touch.clientY >= box.top && touch.clientY <= box.bottom;
    touchState = {x: touch.clientX, y: touch.clientY, target: event.target, inside};
  }, {passive: true});
  document.addEventListener("touchmove", event => {
    if (!lockedPage || !touchState || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = touch.clientX - touchState.x;
    const dy = touch.clientY - touchState.y;
    touchState.x = touch.clientX; touchState.y = touch.clientY;
    if (!dx && !dy) return;
    if (touchState.inside && dialog.open && Math.abs(dy) > Math.abs(dx)) {
      for (let node = touchState.target; node instanceof Element && dialog.contains(node); node = node.parentElement) {
        if (!/^(auto|scroll)$/.test(getComputedStyle(node).overflowY)) continue;
        const remaining = node.scrollHeight - node.clientHeight;
        if (remaining > 1 && (dy < 0 ? node.scrollTop < remaining - 1 : node.scrollTop > 0)) return;
      }
    }
    if (event.cancelable) event.preventDefault();
  }, {passive: false});
  ["touchend", "touchcancel"].forEach(type =>
    document.addEventListener(type, () => { touchState = null; }, {passive: true}));
}

// -- theme -----------------------------------------------------------------
// Telegram's own palette arrives as --tg-theme-*, but the accents for money
// (owed / owing) are ours, and they need different values on a dark ground.
export function paintTheme() {
  const dark = tg?.colorScheme
    ? tg.colorScheme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.scheme = dark ? "dark" : "light";
  document.querySelector("meta[name=theme-color]")
    ?.setAttribute("content", getComputedStyle(document.body).backgroundColor);
  if (!supports("6.1")) return;
  try {
    tg.setHeaderColor("secondary_bg_color");
    tg.setBackgroundColor("secondary_bg_color");
  } catch { /* the client refused the colour */ }
}
