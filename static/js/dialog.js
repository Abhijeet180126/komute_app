/* Keyboard handling for modal dialogs (audit A-5).

   Mark the element that is shown and hidden with data-dialog (plus
   role="dialog" aria-modal="true" and a label). The page keeps opening and
   closing it the way it already does; this file watches for that and:

     - moves focus into the dialog when it opens ([data-dialog-initial] if
       present, otherwise the first focusable element),
     - keeps Tab and Shift+Tab inside the open dialog,
     - presses the dialog's [data-dialog-close] button on Escape (leave the
       attribute off when the page already handles Escape itself),
     - puts focus back on whatever opened the dialog when it closes.

   A dialog counts as open when it isn't display:none, visibility:hidden
   or [hidden]. Loaded from header.html and report-block.html; the guard
   below makes a second copy a no-op. */
(function () {
  'use strict';
  if (window.komuteDialog) return;
  window.komuteDialog = true;

  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
                    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const openStack = [];          // open dialogs, most recent last
  const openers = new WeakMap(); // dialog -> element that had focus before it opened

  function isOpen(d) {
    if (d.hidden) return false;
    const cs = getComputedStyle(d);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  }

  function focusables(d) {
    return Array.from(d.querySelectorAll(FOCUSABLE)).filter(function (el) {
      return el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    });
  }

  function opened(d) {
    if (!openStack.includes(d)) openStack.push(d);
    const active = document.activeElement;
    if (active && !d.contains(active)) openers.set(d, active);
    const first = d.querySelector('[data-dialog-initial]') || focusables(d)[0];
    // Wait a frame so the page's own show code (and any transition) has run.
    requestAnimationFrame(function () {
      if (!isOpen(d) || d.contains(document.activeElement)) return;
      if (first) first.focus(); else { d.setAttribute('tabindex', '-1'); d.focus(); }
    });
  }

  function closed(d) {
    const i = openStack.indexOf(d);
    if (i !== -1) openStack.splice(i, 1);
    const opener = openers.get(d);
    openers.delete(d);
    const active = document.activeElement;
    const lost = !active || active === document.body || d.contains(active);
    if (lost && opener && document.contains(opener) && opener.getClientRects().length) opener.focus();
  }

  function watch(d) {
    let was = isOpen(d);
    if (was) opened(d);
    function sync() {
      const now = isOpen(d);
      if (now === was) return;
      was = now;
      if (now) opened(d); else closed(d);
    }
    new MutationObserver(sync).observe(d, { attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
    // A dialog that slides out (the menu drawer) only becomes visibility:hidden
    // when its transition ends, after the class change.
    d.addEventListener('transitionend', function (e) { if (e.target === d) sync(); });
  }

  document.addEventListener('keydown', function (e) {
    const d = openStack[openStack.length - 1];
    if (!d) return;
    if (e.key === 'Escape') {
      const btn = d.querySelector('[data-dialog-close]');
      if (btn) { e.preventDefault(); btn.click(); }
      return;
    }
    if (e.key !== 'Tab') return;
    const items = focusables(d);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    if (!d.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  function init() { document.querySelectorAll('[data-dialog]').forEach(watch); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
