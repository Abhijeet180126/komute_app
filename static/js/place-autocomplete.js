/* Place autocomplete, shared by post-ride, edit-ride and search-rides
   (ISSUES.md M2). A field only gets coordinates when the user picks one of
   the suggestions: nothing is auto-filled on blur or Tab, so the wrong place
   can't be chosen silently. A failed lookup says so instead of vanishing.

   Where suggestions come from is set by the page's #map-config element
   ({% map_config %}, komute/templatetags/map_tags.py): "photon" (the public
   Photon server, the original) or "apple" (Komute's own /map/ endpoints, which
   ask the map node). Set MAP_AUTOCOMPLETE in .env to switch; no redeploy of
   the script is needed. "Locate Me" still uses placeLabel() with Photon.

   PlaceAutocomplete.attach(input, latInput, lngInput, { onSelect(place) })
   PlaceAutocomplete.hasPlace(latInput, lngInput) -> picked from suggestions?
   PlaceAutocomplete.placeLabel(photonProperties) -> "12 Main St, Toronto, ..."
*/
(function () {
  'use strict';

  const PHOTON_URL = 'https://photon.komoot.io/api/';
  const BBOX = '-168,24,-52,72';   // CONUS + Canada + Alaska (bias only)
  const MIN_CHARS = 3;
  const DEBOUNCE_MS = 300;

  const mapConfig = document.getElementById('map-config');
  const USE_APPLE = !!mapConfig && mapConfig.dataset.autocomplete === 'apple';
  const MAP_URL = '/map';   // Komute's own endpoints (komute/views_map.py), never a third-party host

  // Where the rider probably is, so Apple can rank nearby places first. In order: the page's hint (the origin of their
  // last search or ride), then a GPS fix the browser already allows without asking. This never shows a permission
  // prompt. With neither, the server falls back to its own default and typed full addresses still resolve.
  let biasPromise = null;
  function getBias() {
    if (biasPromise) return biasPromise;
    const lat = parseFloat(mapConfig && mapConfig.dataset.biasLat);
    const lon = parseFloat(mapConfig && mapConfig.dataset.biasLon);
    if (isFinite(lat) && isFinite(lon)) return (biasPromise = Promise.resolve({ lat, lon }));
    biasPromise = new Promise(resolve => {
      if (!navigator.permissions || !navigator.geolocation) return resolve(null);
      navigator.permissions.query({ name: 'geolocation' }).then(p => {
        if (p.state !== 'granted') return resolve(null);
        navigator.geolocation.getCurrentPosition(
          pos => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
          () => resolve(null), { maximumAge: 600000, timeout: 4000 });
      }).catch(() => resolve(null));
    });
    return biasPromise;
  }

  function isInUsCanada(lat, lng) {
    const inMain   = lat >= 24 && lat <= 72 && lng >= -168 && lng <= -52;
    const inHawaii = lat >= 18 && lat <= 23 && lng >= -161 && lng <= -154;
    return inMain || inHawaii;
  }

  // "Union Station, 65 Front Street West, Toronto, Ontario, Canada": the place
  // name (if it isn't just the street or city), house number and street,
  // then city, state and country, without repeats.
  function placeLabel(p) {
    p = p || {};
    const streetLine = p.street ? [p.housenumber, p.street].filter(Boolean).join(' ') : '';
    const city = p.city || p.town || p.village || p.district || p.county;
    const parts = [];
    if (p.name && p.name !== p.street && p.name !== city) parts.push(p.name);
    parts.push(streetLine, city, p.state, p.country);
    const seen = new Set();
    return parts.filter(s => s && !seen.has(s) && seen.add(s)).join(', ');
  }

  function hasPlace(latInput, lngInput) {
    const lat = parseFloat(latInput.value);
    const lng = parseFloat(lngInput.value);
    return isFinite(lat) && isFinite(lng) && !(lat === 0 && lng === 0);
  }

  function attach(input, latInput, lngInput, opts) {
    if (!input || !latInput || !lngInput || input.dataset.placeAc) return;
    input.dataset.placeAc = '1';
    opts = opts || {};
    let timer = null;
    let dropdown = null;
    let controller = null;   // aborts a lookup a newer keystroke replaced

    function close() {
      if (dropdown) { dropdown.remove(); dropdown = null; }
    }

    function open() {
      close();
      const rect = input.getBoundingClientRect();
      dropdown = document.createElement('div');
      dropdown.className = 'autocomplete-dropdown';
      dropdown.style.position = 'absolute';
      dropdown.style.display = 'block';
      dropdown.style.top   = (rect.bottom + window.scrollY + 4) + 'px';
      dropdown.style.left  = (rect.left  + window.scrollX) + 'px';
      dropdown.style.width = rect.width + 'px';
      document.body.appendChild(dropdown);
      return dropdown;
    }

    function note(text, cls) {
      const dd = open();
      const div = document.createElement('div');
      div.className = cls || 'autocomplete-item no-results';
      div.textContent = text;
      dd.appendChild(div);
      return dd;
    }

    function select(place) {
      input.value = place.label;
      latInput.value = place.lat;
      lngInput.value = place.lng;
      input.classList.add('place-confirmed');
      close();
      if (opts.onSelect) opts.onSelect(place);
    }

    function lookup(q) {
      if (USE_APPLE) return lookupApple(q);
      if (controller) controller.abort();
      controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      note('Searching…', 'autocomplete-loading');
      fetch(PHOTON_URL + '?q=' + encodeURIComponent(q) + '&limit=5&lang=en&bbox=' + BBOX,
            controller ? { signal: controller.signal } : undefined)
        .then(r => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(data => {
          if (input.value.trim() !== q) return;   // user kept typing
          const places = (data.features || [])
            .map(f => ({ label: placeLabel(f.properties), lat: f.geometry.coordinates[1], lng: f.geometry.coordinates[0] }))
            .filter(pl => pl.label && isInUsCanada(pl.lat, pl.lng));
          if (!places.length) {
            note('No US/Canada matches. Try adding the street, city or province/state.');
            return;
          }
          const dd = open();
          places.forEach(pl => {
            const item = document.createElement('div');
            item.className = 'autocomplete-item';
            item.textContent = pl.label;
            // mousedown, not click: fires before the input's blur closes the list
            item.addEventListener('mousedown', e => { e.preventDefault(); select(pl); });
            dd.appendChild(item);
          });
        })
        .catch(err => {
          if (err && err.name === 'AbortError') return;
          if (input.value.trim() !== q) return;
          const dd = note("Couldn't load suggestions. Check your connection.");
          const retry = document.createElement('div');
          retry.className = 'autocomplete-item';
          retry.textContent = '↻ Try again';
          retry.addEventListener('mousedown', e => { e.preventDefault(); lookup(q); });
          dd.appendChild(retry);
        });
    }

    // Suggestions from Komute's /map/suggest/ (the map node, Apple Maps). Same box, same keys, same wording as the
    // Photon path above; a row without coordinates is resolved when it is picked.
    function lookupApple(q) {
      if (controller) controller.abort();
      controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      note('Searching…', 'autocomplete-loading');
      getBias()
        .then(bias => fetch(MAP_URL + '/suggest/?q=' + encodeURIComponent(q) + (bias ? '&lat=' + bias.lat + '&lon=' + bias.lon : ''),
                            { credentials: 'same-origin', signal: controller ? controller.signal : undefined }))
        .then(r => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(data => {
          if (input.value.trim() !== q) return;   // user kept typing
          const places = (data.results || [])
            .map(r => ({ label: [r.title, r.subtitle].filter(Boolean).join(', '), ref: r.ref, lat: r.lat, lng: r.lon }))
            .filter(pl => pl.label && (pl.lat == null || isInUsCanada(pl.lat, pl.lng)));
          if (!places.length) {
            note('No US/Canada matches. Try adding the street, city or province/state.');
            return;
          }
          const dd = open();
          places.forEach(pl => {
            const item = document.createElement('div');
            item.className = 'autocomplete-item';
            item.textContent = pl.label;
            // mousedown, not click: fires before the input's blur closes the list
            item.addEventListener('mousedown', e => { e.preventDefault(); pickApple(pl); });
            dd.appendChild(item);
          });
        })
        .catch(err => {
          if (err && err.name === 'AbortError') return;
          if (input.value.trim() !== q) return;
          const dd = note("Couldn't load suggestions. Check your connection.");
          const retry = document.createElement('div');
          retry.className = 'autocomplete-item';
          retry.textContent = '↻ Try again';
          retry.addEventListener('mousedown', e => { e.preventDefault(); lookup(q); });
          dd.appendChild(retry);
        });
    }

    function pickApple(pl) {
      if (pl.lat != null) return select(pl);
      fetch(MAP_URL + '/details/?ref=' + encodeURIComponent(pl.ref), { credentials: 'same-origin' })
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(d => select({ label: pl.label, lat: d.lat, lng: d.lon }))
        .catch(() => note("Couldn't load that place. Pick another or type the full address."));
    }

    input.addEventListener('input', () => {
      // Edited text no longer matches the picked place.
      latInput.value = '';
      lngInput.value = '';
      input.classList.remove('place-confirmed');
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < MIN_CHARS) {
        if (controller) controller.abort();
        close();
        return;
      }
      timer = setTimeout(() => lookup(q), DEBOUNCE_MS);
    });

    input.addEventListener('blur', () => setTimeout(close, 150));

    input.addEventListener('keydown', e => {
      if (!dropdown) return;
      const items = [...dropdown.querySelectorAll('.autocomplete-item:not(.no-results)')];
      const active = dropdown.querySelector('.autocomplete-item.active');
      const idx = items.indexOf(active);
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && items.length) {
        e.preventDefault();
        if (active) active.classList.remove('active');
        const step = e.key === 'ArrowDown' ? 1 : -1;
        items[(idx + step + items.length) % items.length].classList.add('active');
      } else if (e.key === 'Enter' && active) {
        e.preventDefault();
        active.dispatchEvent(new MouseEvent('mousedown'));
      } else if (e.key === 'Tab' && active) {
        // Only a highlighted suggestion is taken; Tab alone just moves on.
        active.dispatchEvent(new MouseEvent('mousedown'));
      } else if (e.key === 'Escape') {
        close();
      }
    });
  }

  window.PlaceAutocomplete = { attach, hasPlace, placeLabel, isInUsCanada, BBOX };
})();
