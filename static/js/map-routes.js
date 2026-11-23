/* Drive time and distance for the post-ride and edit-ride forms.

   KomuteRoutes.summary(points, { direct }) -> Promise of
     { distance, duration, directDistance }   (metres, seconds; directDistance is null unless asked for)
   points: [{ lat, lng }, ...] in driving order (start, stops, end). With direct: true and at least one stop it
   also returns the start-to-end distance, which the post-ride detour warning compares against.
   A trip with no drivable road rejects with an Error whose noRoute is true; anything else rejects plainly.

   Where routes come from is set by the page's #map-config element ({% map_config %}): "osrm" (the public OSRM
   demo server, the original) or "apple" (Komute's own /map/route/, which asks the map node). MAP_ROUTES in .env
   switches it. This script only returns numbers: the route maps on the trip pages draw their own line.
*/
(function () {
  'use strict';

  const cfg = document.getElementById('map-config');
  const PROVIDER = cfg && cfg.dataset.routes === 'apple' ? 'apple' : 'osrm';
  const OSRM_URL = 'https://router.project-osrm.org/route/v1/driving/';
  const ROUTE_URL = '/map/route/';

  function noRoute() {
    const e = new Error('No drivable route was found.');
    e.noRoute = true;
    return e;
  }

  // OSRM wants "lng,lat;lng,lat".
  async function osrmRoute(points) {
    const coords = points.map(p => p.lng + ',' + p.lat).join(';');
    const res = await fetch(OSRM_URL + coords + '?overview=false');
    const data = await res.json();
    if (data.code !== 'Ok' || !data.routes || !data.routes.length) throw noRoute();
    return { distance: data.routes[0].distance, duration: data.routes[0].duration };
  }

  async function osrmSummary(points, direct) {
    const wantDirect = direct && points.length > 2;
    const [main, straight] = await Promise.all([
      osrmRoute(points),
      // The start-to-end route is only a hint for the detour warning; if it fails, there is simply no warning.
      wantDirect ? osrmRoute([points[0], points[points.length - 1]]).catch(() => null) : null,
    ]);
    return { distance: main.distance, duration: main.duration, directDistance: straight ? straight.distance : null };
  }

  // Komute's endpoint wants "lat,lng", stops joined with ";".
  async function appleSummary(points, direct) {
    const ll = p => p.lat + ',' + p.lng;
    const q = new URLSearchParams({ o: ll(points[0]), d: ll(points[points.length - 1]) });
    if (points.length > 2) q.set('s', points.slice(1, -1).map(ll).join(';'));
    if (direct && points.length > 2) q.set('direct', '1');
    const res = await fetch(ROUTE_URL + '?' + q.toString(), { credentials: 'same-origin' });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON: handled below */ }
    if (res.status === 422 && data && data.code === 'no_route') throw noRoute();
    if (!res.ok || !data || typeof data.distance_m !== 'number' || typeof data.duration_s !== 'number') {
      throw new Error('Route request failed (HTTP ' + res.status + ')');
    }
    return {
      distance: data.distance_m,
      duration: data.duration_s,
      directDistance: typeof data.direct_distance_m === 'number' ? data.direct_distance_m : null,
    };
  }

  function summary(points, opts) {
    const direct = !!(opts && opts.direct);
    return PROVIDER === 'apple' ? appleSummary(points, direct) : osrmSummary(points, direct);
  }

  window.KomuteRoutes = { summary, provider: PROVIDER };
})();
