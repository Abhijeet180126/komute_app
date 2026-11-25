// Part 3, UI half: type into the address box and tap Locate Me with a fake GPS, on today's page (Photon) and the Apple page.
// Needs lab_server.py running on :8765. Writes qa/out/t3_ui.json and screenshots.  node qa/t3_ui.mjs
import { writeFileSync } from 'node:fs';
import { init, sleep } from './cdp.mjs';

const BASE = process.env.LAB || 'http://localhost:8765';
const TYPE = [  // what is typed, where the rider is (the Apple page sends this as its location bias)
  { id: 'toronto', typed: '100 queen', bias: [43.6532, -79.3832], want: /100 Queen St(reet)? W|100 Queen Street West/i, label: 'Rider in downtown Toronto types "100 queen"' },
  { id: 'kingston', typed: '216 ontar', bias: [44.2312, -76.4860], want: /216 Ontario/i, label: 'Rider in Kingston types "216 ontar"' },
];
const GPS = [
  { id: 'toronto', at: [43.6532, -79.3832], label: 'Downtown Toronto' },
  { id: 'vancouver', at: [49.2827, -123.1207], label: 'Downtown Vancouver' },
  { id: 'wawa', at: [47.9916, -84.7727], label: 'Trans-Canada Hwy near Wawa, ON (rural)' },
  { id: 'buffalo', at: [42.8864, -78.8784], label: 'Buffalo, NY (outside Canada)' },
];
const out = { generated: new Date().toISOString().slice(0, 16).replace('T', ' '), typing: [], locate: [] };
const page = await init({ width: 700, height: 520 });

for (const kind of ['photon', 'apple']) {
  for (const t of TYPE) {
    page.resetLogs();
    await page.goto(`${BASE}/lab/places-${kind}.html`);
    if (kind === 'apple') await page.eval(`window.PLACE_BIAS = { lat: ${t.bias[0]}, lon: ${t.bias[1]} }`);
    await page.type('#origin_input', t.typed, 70);
    const t0 = Date.now();
    const ms = await page.waitFor(`document.querySelectorAll('.autocomplete-item').length > 0 && !document.querySelector('.autocomplete-loading')`, 10000);
    await sleep(250);
    const items = await page.eval(`[...document.querySelectorAll('.autocomplete-item')].map(e => e.textContent)`);
    const rank = items.findIndex((x) => t.want.test(x)) + 1 || null;
    const shot = `qa/out/shots/t3-type-${t.id}-${kind}.png`;
    await page.shot(shot);
    out.typing.push({ provider: kind, scenario: t.id, label: t.label, typed: t.typed, after_last_key_ms: ms === null ? null : Date.now() - t0 - 250, first: items[0] ?? null, right_rank: rank, items, screenshot: shot });
    console.log(kind, t.id, 'first:', items[0], '| right answer rank', rank);
  }
  for (const g of GPS) {
    page.resetLogs();
    await page.goto(`${BASE}/lab/places-${kind}.html`);
    await page.geo(g.at[0], g.at[1]);
    await page.eval('window.QA.locate = undefined');
    await page.click('#locateBtn');
    await page.waitFor('window.QA.locate && window.QA.locate.ms', 15000);
    await sleep(200);
    const loc = JSON.parse(await page.eval('JSON.stringify(window.QA.locate || {})'));
    const shown = await page.eval('document.getElementById("result").textContent');
    const shot = `qa/out/shots/t3-locate-${g.id}-${kind}.png`;
    await page.shot(shot);
    out.locate.push({ provider: kind, scenario: g.id, label: g.label, ms: loc.ms ?? null, refused: !!loc.refused, address: loc.label ?? null, error: loc.error ?? null, shown: shown.slice(0, 140), screenshot: shot });
    console.log(kind, 'locate', g.id, '->', loc.refused ? 'REFUSED' : loc.label, '|', loc.ms, 'ms');
  }
}
await page.close();
writeFileSync('qa/out/t3_ui.json', JSON.stringify(out, null, 1));
console.log('t3 ui done');
