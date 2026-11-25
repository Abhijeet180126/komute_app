// "Before" screenshots from Komute's REAL pages (today's Leaflet + OSM tiles + OSRM + Photon), on the throwaway database
// that seed_real_pages.py serves on :8770. Writes qa/out/real_pages_result.json and screenshots.  node qa/real_pages.mjs
import { readFileSync, writeFileSync } from 'node:fs';
import { init, sleep } from './cdp.mjs';

const PFX = process.env.SHOT_PREFIX || 'real';  // 'real' = today's pages; use 'after' once a switch is on
const info = JSON.parse(readFileSync('qa/out/real_pages.json', 'utf8'));
const out = { generated: new Date().toISOString().slice(0, 16).replace('T', ' '), pages: [] };
const hostsOf = (reqs) => { const h = {}; for (const r of reqs) { try { const u = new URL(r.url); if (!u.host.startsWith('127.0.0.1')) h[u.host] = (h[u.host] || 0) + 1; } catch { /* data: url */ } } return h; };

async function open(page, who, path, w = 1100, h = 800, mobile = false) {
  await page.viewport(w, h, mobile);
  await page.send('Network.setCookie', { name: 'sessionid', value: info.cookies[who], domain: '127.0.0.1', path: '/' });
  page.resetLogs();
  const ms = await page.goto(info.base + path);
  return ms;
}
const errors = (page) => page.console.filter((c) => c.level === 'error' && !/favicon|Failed to load resource.*(404|403)/.test(c.text)).map((c) => c.text.slice(0, 160)).slice(0, 4);

const page = await init({ width: 1100, height: 800 });

// 1. ride summary (driver, completed ride) and booking details (rider, open ride): Leaflet + OSM tiles + OSRM line
for (const [pname, who, vname, w, h, mobile] of [['ride-summary', 'driver', 'desktop', 1100, 800, false], ['ride-summary', 'driver', 'phone', 390, 844, true], ['booking-details', 'rider', 'desktop', 1100, 800, false], ['booking-details', 'rider', 'phone', 390, 844, true]]) {
  const t0 = Date.now();
  await open(page, who, info.pages[pname], w, h, mobile);
  const ok = await page.waitFor(`document.getElementById('routeMapStats') && getComputedStyle(document.getElementById('routeMapStats')).display !== 'none'`, 25000);
  await sleep(2500);  // let the tiles land
  await page.eval(`document.getElementById('routeMap')?.scrollIntoView({block:'center'})`); await sleep(500);
  const shot = `qa/out/shots/${PFX}-${pname}-${vname}.png`;
  await page.shot(shot);
  const reqs = [...page.requests.values()];
  out.pages.push({ page: pname, view: vname, route_stats_shown: ok !== null, ready_ms: Date.now() - t0, stats: await page.eval(`document.getElementById('routeMapStats')?.innerText.replace(/\\s+/g,' ')`),
    tiles: reqs.filter((r) => r.url.includes('tile.openstreetmap.org')).length, osrm_calls: reqs.filter((r) => r.url.includes('router.project-osrm.org')).length, hosts: hostsOf(reqs), console_errors: errors(page), screenshot: shot });
  console.log(pname, vname, 'stats:', out.pages.at(-1).stats, '| tiles', out.pages.at(-1).tiles);
}

// 2. post-ride and 3. search-rides: autocomplete + Locate Me
const FORMS = [
  { page: 'post-ride', who: 'driver', pre: 'showForm()', input: '#origin_input', locate: '#locateBtn', result: `document.getElementById('origin_input').value` },
  { page: 'search-rides', who: 'rider', input: '#searchFrom', locate: '#locateBtn', result: `document.getElementById('searchFrom').value` },
];
for (const f of FORMS) {
  await open(page, f.who, info.pages[f.page]);
  if (f.pre) { await sleep(800); await page.eval(f.pre); }  // post-ride shows its form only after "Post a New Ride" is pressed
  await page.waitFor(`document.querySelector(${JSON.stringify(f.input)})?.offsetParent`, 20000);
  await sleep(500);
  const landed = await page.eval('location.pathname');
  const present = await page.eval(`!!document.querySelector(${JSON.stringify(f.input)})`);
  const rec = { page: f.page, landed, input_present: present };
  if (present) {
    await page.type(f.input, '100 queen', 70);
    const ms = await page.waitFor(`document.querySelectorAll('.autocomplete-item').length > 0 && !document.querySelector('.autocomplete-loading')`, 10000);
    await sleep(300);
    rec.suggestions = await page.eval(`[...document.querySelectorAll('.autocomplete-item')].map(e => e.textContent)`);
    rec.suggest_ms = ms;
    rec.type_shot = `qa/out/shots/${PFX}-${f.page}-type.png`;
    await page.shot(rec.type_shot);
    await page.eval('document.activeElement.blur()'); await sleep(400);
    await page.eval(`document.querySelector(${JSON.stringify(f.input)}).value = ''`);
    await page.geo(43.6532, -79.3832);
    page.resetLogs();
    await page.eval(`window.alert = (m) => { window.__alert = m; }`);
    await page.click(f.locate);
    await page.waitFor(`${f.result}.length > 0`, 12000);
    await sleep(500);
    rec.locate_label = await page.eval(f.result);
    rec.locate_alert = await page.eval('window.__alert || null');
    rec.locate_shot = `qa/out/shots/${PFX}-${f.page}-locate.png`;
    await page.shot(rec.locate_shot);
    rec.photon_calls = [...page.requests.values()].filter((r) => r.url.includes('photon.komoot.io')).length;
  } else {
    rec.note = 'Input not found; the page redirected. Screenshot taken for the record.';
    rec.type_shot = `qa/out/shots/${PFX}-${f.page}-redirect.png`;
    await page.shot(rec.type_shot);
  }
  rec.console_errors = errors(page);
  out.pages.push(rec);
  console.log(f.page, 'landed', landed, '| input', present, '| first suggestion:', rec.suggestions?.[0], '| locate:', rec.locate_label);
}
await page.close();
writeFileSync(`qa/out/${PFX}_pages_result.json`, JSON.stringify(out, null, 1));
console.log('real pages done');
