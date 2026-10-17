/* Report and Block another user (ISSUES.md A1). The dialogs come from
   {% report_block_dialogs %} (templates/report-block.html). A button opens
   one with komuteSafety.open(this) and these data attributes:

     data-safety="report" | "block"
     data-booking="<booking hashid>"  or  data-ride="<ride hashid>" (its driver)
     data-name="<the other user's name>"
     data-after-block="reload" | "<url>"   optional; default shows a confirmation

   A successful block also fires a "komute:blocked" event on document.
*/
(function () {
  'use strict';

  const root = document.getElementById('rbRoot');
  if (!root) return;

  let target = null;

  function el(id) { return document.getElementById(id); }

  function csrfToken() {
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function showDialog(id) {
    root.querySelectorAll('.rb-backdrop').forEach(function (d) { d.classList.remove('show'); });
    el(id).classList.add('show');
  }

  function close() {
    root.querySelectorAll('.rb-backdrop').forEach(function (d) { d.classList.remove('show'); });
  }

  function setError(id, message) {
    const box = el(id);
    box.textContent = message || '';
    box.classList.toggle('show', !!message);
  }

  async function post(url, fields) {
    const body = new FormData();
    if (target.booking) body.append('booking', target.booking);
    if (target.ride) body.append('ride', target.ride);
    Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
    let data = {};
    let resp;
    try {
      resp = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
        body: body,
      });
      data = await resp.json();
    } catch (e) {
      throw new Error('Network error. Please try again.');
    }
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Something went wrong. Please try again.');
    return data;
  }

  function open(btn) {
    target = {
      booking: btn.dataset.booking || '',
      ride: btn.dataset.ride || '',
      name: btn.dataset.name || 'this user',
      afterBlock: btn.dataset.afterBlock || '',
      blocked: btn.dataset.blocked === 'true',
    };
    // textContent, never innerHTML: the name is free text the user typed.
    root.querySelectorAll('[data-rb-name]').forEach(function (s) { s.textContent = target.name; });
    if (btn.dataset.safety === 'block') openBlock(); else openReport();
  }

  function openReport() {
    root.querySelectorAll('input[name="rbReason"]').forEach(function (r) { r.checked = false; });
    el('rbDetails').value = '';
    el('rbDetailsCount').textContent = '0';
    setError('rbReportError', '');
    el('rbReportForm').hidden = false;
    el('rbReportDone').hidden = true;
    el('rbReportSubmit').disabled = false;
    showDialog('rbReport');
  }

  async function submitReport() {
    const picked = root.querySelector('input[name="rbReason"]:checked');
    const details = el('rbDetails').value.trim();
    if (!picked) { setError('rbReportError', 'Please choose a reason.'); return; }
    if (picked.value === 'other' && !details) { setError('rbReportError', 'Please tell us what happened.'); return; }
    setError('rbReportError', '');
    el('rbReportSubmit').disabled = true;
    try {
      await post(root.dataset.reportUrl, { reason: picked.value, details: details });
    } catch (e) {
      setError('rbReportError', e.message);
      el('rbReportSubmit').disabled = false;
      return;
    }
    el('rbReportForm').hidden = true;
    el('rbReportDone').hidden = false;
    el('rbReportBlockHint').hidden = target.blocked;
    el('rbReportBlockBtn').hidden = target.blocked;
  }

  function openBlock() {
    setError('rbBlockError', '');
    el('rbBlockForm').hidden = false;
    el('rbBlockDone').hidden = true;
    el('rbBlockSubmit').disabled = false;
    showDialog('rbBlock');
  }

  async function submitBlock() {
    el('rbBlockSubmit').disabled = true;
    try {
      await post(root.dataset.blockUrl, {});
    } catch (e) {
      setError('rbBlockError', e.message);
      el('rbBlockSubmit').disabled = false;
      return;
    }
    target.blocked = true;
    document.dispatchEvent(new CustomEvent('komute:blocked', { detail: { booking: target.booking, ride: target.ride } }));
    if (target.afterBlock === 'reload') { location.reload(); return; }
    if (target.afterBlock) { location.href = target.afterBlock; return; }
    el('rbBlockForm').hidden = true;
    el('rbBlockDone').hidden = false;
  }

  el('rbDetails').addEventListener('input', function () {
    el('rbDetailsCount').textContent = this.value.length;
  });
  root.querySelectorAll('.rb-backdrop').forEach(function (d) {
    d.addEventListener('click', function (e) { if (e.target === d) close(); });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') close();
  });

  window.komuteSafety = {
    open: open,
    openBlock: openBlock,
    close: close,
    submitReport: submitReport,
    submitBlock: submitBlock,
  };
})();
