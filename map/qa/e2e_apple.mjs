// End-to-end check of Komute's REAL pages with every map switch on Apple (MAP_AUTOCOMPLETE=apple, MAP_ROUTES=apple,
// MAP_DISPLAY=mapkit). Needs the map node on :8081 and seed_real_pages.py on :8770 started with those switches.
//   node qa/e2e_apple.mjs      (from map/)   Writes qa/out/e2e_apple_result.json and qa/out/shots/e2e-*.png.
// Exit code 1 if any check fails.
import { readFileSync, writeFileSync } from 'node:fs';
import { init, sleep } from './cdp.mjs';

const info = JSON.parse(readFileSync('qa/out/real_pages.json', 'utf8'));
const page = await init({ width: 1100, height: 900 });
// CSP is report-only on the dev site: record every violation the page would have had under enforcement.
await page.send('Page.addScriptToEvaluateOnNewDocument', { source:
  "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.effectiveDirective + ' ' + e.blockedURI));" });

const results = [];
let failed = 0;
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail });
  if (!ok) failed++;
  console.log(ok ? 'PASS' : 'FAIL', name, detail === undefined ? '' : JSON.stringify(detail));
}
async function open(who, path) {
  await page.send('Network.setCookie', { name: 'sessionid', value: info.cookies[who], domain: '127.0.0.1', path: '/' });
  page.resetLogs();
  await page.goto(info.base + path);
}
const reqs = () => [...page.requests.values()];
const hits = (s) => reqs().filter((r) => r.url.includes(s));
const errors = () => page.console.filter((c) => c.level === 'error' && !/favicon/.test(c.text)).map((c) => c.text.slice(0, 200))
  .concat(page.errors || []);
async function common(label) {
  check(`${label}: no OSRM, OSM tile, unpkg or Photon-search calls`,
    !hits('router.project-osrm.org').length && !hits('tile.openstreetmap.org').length && !hits('unpkg.com').length
      && !hits('photon.komoot.io/api').length, Object.keys(Object.fromEntries(reqs().map((r) => [new URL(r.url).host, 1]))));
  check(`${label}: no CSP violations`, (await page.eval('window.__csp')).length === 0, await page.eval('window.__csp'));
  check(`${label}: no console errors`, errors().length === 0, errors().slice(0, 5));
}

// Pick a place through the real autocomplete: type, wait for Apple suggestions, press the first one.
async function pick(selector, text, latSel) {
  await page.eval(`document.querySelector(${JSON.stringify(selector)}).value = ''`);
  await page.type(selector, text, 50);
  await page.waitFor(`[...document.querySelectorAll('.autocomplete-item')].some(e => !e.classList.contains('no-results') && e.offsetParent)`, 12000);
  const first = await page.eval(`[...document.querySelectorAll('.autocomplete-item')].find(e => e.offsetParent && !e.classList.contains('no-results')).textContent`);
  await page.eval(`[...document.querySelectorAll('.autocomplete-item')].find(e => e.offsetParent && !e.classList.contains('no-results'))
    .dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))`);
  const ok = await page.waitFor(`${latSel} && ${latSel}.value !== ''`, 12000);
  return { first, ok: ok !== null };
}

// ---- 1. the trip pages: MapKit map, Apple route line, stats -------------------------------------------------------------
for (const [pname, who] of [['ride-summary', 'driver'], ['booking-details', 'rider']]) {
  await open(who, info.pages[pname]);
  const shown = await page.waitFor(`getComputedStyle(document.getElementById('routeMapStats')).display !== 'none'`, 25000);
  await sleep(3000);  // map tiles
  await page.eval(`document.getElementById('routeMap').scrollIntoView({block:'center'})`); await sleep(800);
  await page.shot(`qa/out/shots/e2e-${pname}.png`);
  check(`${pname}: MapKit loaded`, await page.eval('!!window.mapkit && !window.L'));
  check(`${pname}: map drawn`, await page.eval(`!!document.querySelector('#routeMap canvas, #routeMap .mk-map-view')`));
  check(`${pname}: route stats shown`, shown !== null, await page.eval(`document.getElementById('routeMapStats').innerText.replace(/\\s+/g,' ')`));
  check(`${pname}: token and route came from Komute`, hits('/map/token/').some((r) => r.status === 200) && hits('/map/route/').some((r) => r.status === 200),
    { token: hits('/map/token/').map((r) => r.status), route: hits('/map/route/').map((r) => r.status) });
  check(`${pname}: route request carried the stop`, hits('/map/route/').some((r) => r.url.includes('s=44.2312')), hits('/map/route/').map((r) => r.url.split('?')[1]));
  await common(pname);
}

// ---- 2. post-ride: autocomplete -> route strip -> duration_seconds; a stop -> detour warning ---------------------------
await open('driver', info.pages['post-ride']);
await sleep(800);
await page.eval('showForm()');
await page.waitFor(`document.querySelector('#origin_input')?.offsetParent`, 15000);
let p = await pick('#origin_input', '100 Queen St W Toronto', `document.getElementById('origin_lat')`);
check('post-ride: origin picked', p.ok, p.first);
p = await pick('#destination_input', '111 Wellington St Ottawa', `document.getElementById('destination_lat')`);
check('post-ride: destination picked', p.ok, p.first);
const strip = await page.waitFor(`/km/.test(document.getElementById('routeSummary').innerText) && document.getElementById('duration_seconds').value !== ''`, 20000);
const direct = { strip: await page.eval(`document.getElementById('routeSummary').innerText.replace(/\\s+/g,' ')`),
  duration_seconds: Number(await page.eval(`document.getElementById('duration_seconds').value`)) };
check('post-ride: route strip and duration_seconds', strip !== null && direct.duration_seconds > 3 * 3600 && direct.duration_seconds < 7 * 3600, direct);
await page.shot('qa/out/shots/e2e-post-ride-route.png');

// A stop well off the Toronto -> Ottawa line (Barrie) must raise the detour warning.
const before = hits('/map/route/').length;
await page.eval(`addStop('pickup')`);
await sleep(300);
p = await pick('#pickupStops .stop-location-input', 'Barrie City Hall', `document.querySelector('#pickupStops [name="stop_lat"]')`);
check('post-ride: stop picked', p.ok, p.first);
const warned = await page.waitFor(`getComputedStyle(document.getElementById('stopDetourWarning')).display !== 'none'`, 20000);
const withStop = { duration_seconds: Number(await page.eval(`document.getElementById('duration_seconds').value`)),
  warning: await page.eval(`document.getElementById('stopDetourWarning').innerText.replace(/\\s+/g,' ').slice(0, 140)`) };
check('post-ride: detour warning for the off-route stop', warned !== null, withStop);
check('post-ride: stop route is longer', withStop.duration_seconds > direct.duration_seconds, withStop);
check('post-ride: stop route used direct=1', hits('/map/route/').slice(before).some((r) => r.url.includes('direct=1') && r.url.includes('s=')),
  hits('/map/route/').slice(before).map((r) => r.url.split('?')[1]));
await page.shot('qa/out/shots/e2e-post-ride-detour.png', { full: true });
await common('post-ride');

// ---- 3. edit-ride: the saved trip's summary comes from Apple on load; a new destination recalculates it ------------------
await open('driver', `/ride-edit/${info.ride_id}/`);
const loaded = await page.waitFor(`/km/.test(document.getElementById('routeSummary').innerText)`, 20000);
const firstSummary = await page.eval(`document.getElementById('routeSummary').innerText.replace(/\\s+/g,' ')`);
check('edit-ride: summary on load', loaded !== null, firstSummary);
p = await pick('#destination_input', '216 Ontario St Kingston ON', `document.getElementById('destination_lat')`);
check('edit-ride: new destination picked', p.ok, p.first);
const changed = await page.waitFor(`/km/.test(document.getElementById('routeSummary').innerText) && document.getElementById('routeSummary').innerText.replace(/\\s+/g,' ') !== ${JSON.stringify(firstSummary)}`, 20000);
check('edit-ride: summary recalculated', changed !== null, await page.eval(`document.getElementById('routeSummary').innerText.replace(/\\s+/g,' ')`));
await page.shot('qa/out/shots/e2e-edit-ride.png');
await common('edit-ride');

await page.close();
writeFileSync('qa/out/e2e_apple_result.json', JSON.stringify({ generated: new Date().toISOString(), failed, results }, null, 1));
console.log(failed ? `${failed} FAILED` : 'ALL PASSED');
process.exit(failed ? 1 : 0);
