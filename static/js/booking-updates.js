/* Live "your bookings changed" notice for the inboxes and my-rides
   (ISSUES.md M4). Opens the per-user socket (/ws/notify/). When the other
   party requests, accepts, rejects or cancels a booking, a banner offers a
   refresh instead of re-rendering cards that the server builds.

   <script src="{% static 'js/booking-updates.js' %}"
           data-fingerprint="{{ bookings_fingerprint }}"></script>

   The server sends its own fingerprint when the socket connects; if it
   differs from the one the page was rendered with, something changed while
   the page loaded or the socket was down, so the banner shows too.
*/
(function () {
  'use strict';

  const script = document.currentScript;
  const PAGE_FINGERPRINT = script ? script.dataset.fingerprint : '';
  const RETRY_MAX_MS = 30000;
  const HEARTBEAT_MS = 25000;   // well under Nginx proxy_read_timeout (120s)
  const CLOSE_NOT_SIGNED_IN = 4401;

  let socket = null;
  let retryDelay = 1000;
  let retryTimer = null;
  let heartbeatTimer = null;
  let awaitingPong = false;
  let stopped = false;
  let pageLeaving = false;
  let banner = null;

  function showBanner(message) {
    if (!banner) {
      banner = document.createElement('div');
      banner.setAttribute('role', 'status');
      banner.setAttribute('aria-live', 'polite');
      banner.style.cssText =
        // Top, under the sticky nav: the pages' own toasts use the bottom.
        'position:fixed;left:50%;top:84px;transform:translateX(-50%);z-index:10000;' +
        'display:flex;align-items:center;gap:12px;max-width:calc(100% - 32px);' +
        'padding:12px 14px 12px 18px;border-radius:12px;background:#1f2937;color:#fff;' +
        'font-family:inherit;font-size:14px;font-weight:500;line-height:1.4;box-shadow:0 8px 24px rgba(0,0,0,.25);';
      const text = document.createElement('span');
      text.className = 'bu-text';
      const refresh = document.createElement('button');
      refresh.type = 'button';
      refresh.textContent = 'Refresh';
      refresh.style.cssText =
        'flex:none;border:0;border-radius:8px;padding:7px 14px;cursor:pointer;' +
        'background:#fff;color:#1f2937;font-family:inherit;font-size:13px;font-weight:600;line-height:1;';
      refresh.addEventListener('click', function () { location.reload(); });
      const close = document.createElement('button');
      close.type = 'button';
      close.setAttribute('aria-label', 'Dismiss');
      close.textContent = '×';
      close.style.cssText =
        'flex:none;border:0;background:none;color:#fff;font-size:20px;line-height:1;' +
        'cursor:pointer;padding:0 4px;opacity:.7;';
      close.addEventListener('click', function () { banner.style.display = 'none'; });
      banner.append(text, refresh, close);
      document.body.appendChild(banner);
    }
    banner.querySelector('.bu-text').textContent = message + ' Refresh to see the latest.';
    banner.style.display = 'flex';
  }

  function connect() {
    if (socket || stopped) return;
    clearTimeout(retryTimer);
    retryTimer = null;

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(protocol + '://' + location.host + '/ws/notify/');
    socket = ws;

    ws.onopen = function () {
      if (ws !== socket) return;
      retryDelay = 1000;
      startHeartbeat();
    };

    ws.onmessage = function (e) {
      if (ws !== socket) return;
      awaitingPong = false;
      let data;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      if (data.type === 'state') {
        if (PAGE_FINGERPRINT && data.fingerprint !== PAGE_FINGERPRINT) {
          showBanner('Your bookings have changed since this page loaded.');
        }
      } else if (data.type === 'bookings_changed') {
        showBanner(data.message || 'Your bookings have changed.');
      }
    };

    ws.onclose = function (event) {
      if (ws !== socket) return;
      socket = null;
      stopHeartbeat();
      if (pageLeaving) return;
      if (event.code === CLOSE_NOT_SIGNED_IN) { stopped = true; return; }
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (retryTimer || stopped) return;
    retryTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS);
  }

  function retryNow() {
    if (socket || stopped) return;
    retryDelay = 1000;
    connect();
  }

  function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(function () {
      if (!socket) return;
      if (awaitingPong) {
        // No reply since the last ping: the connection is dead.
        const dead = socket;
        socket = null;
        stopHeartbeat();
        try { dead.close(); } catch (_) {}
        scheduleReconnect();
        return;
      }
      awaitingPong = true;
      try { socket.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
    }, HEARTBEAT_MS);
  }

  function stopHeartbeat() {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    awaitingPong = false;
  }

  window.addEventListener('online', retryNow);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') retryNow();
  });
  window.addEventListener('pagehide', function () { pageLeaving = true; });

  if (window.WebSocket) connect();
})();
