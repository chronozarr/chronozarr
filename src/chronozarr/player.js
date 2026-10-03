// anywidget frontend using only the versioned iframe postMessage contract.
export default {
  render({ model, el }) {
    const box = document.createElement('div');
    box.style.cssText = 'display:grid;gap:8px;font:13px system-ui';
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;gap:10px;align-items:center;flex-wrap:wrap';
    const play = document.createElement('button'); play.textContent = 'Play';
    const time = document.createElement('input'); time.type = 'range'; time.min = '0'; time.max = '0';
    time.setAttribute('aria-label', 'Timestep'); time.style.flex = '1';
    const date = document.createElement('span');
    const product = document.createElement('select'); product.setAttribute('aria-label', 'Product');
    const band = document.createElement('select'); band.setAttribute('aria-label', 'Band');
    const low = document.createElement('input'), high = document.createElement('input');
    for (const [input, label] of [[low, 'Display minimum'], [high, 'Display maximum']]) { input.type = 'number'; input.step = 'any'; input.setAttribute('aria-label', label); input.style.width = '90px'; }
    const auto = document.createElement('button'); auto.textContent = 'Auto limits';
    const legend = document.createElement('span'); legend.setAttribute('aria-label', 'Units legend');
    const speed = document.createElement('select'); speed.setAttribute('aria-label', 'Playback speed');
    for (const value of [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,20,24,30,40,48,60]) {
      speed.add(new Option(`${value}/s`, String(value)));
    }
    const status = document.createElement('div'); status.setAttribute('role', 'status');
    const frame = document.createElement('iframe'); frame.title = 'chronozarr player';
    frame.style.cssText = 'width:100%;border:0;min-height:320px';
    frame.allow = 'fullscreen; local-network-access';
    controls.append(play, time, date, product, band, low, high, auto, legend, speed); box.append(controls, frame, status); el.append(box);
    let origin, connected = false, disposed = false, suppress = false, saveTimer = 0, commandTimer = 0;
    let pending = {};
    let activeBand = '';
    const bandRanges = new Map();
    const subscriptions = [];
    const save = () => {
      if (!saveTimer) saveTimer = setTimeout(() => { saveTimer = 0; model.save_changes(); }, 100);
    };
    const update = values => {
      suppress = true;
      try { for (const [key, value] of Object.entries(values)) model.set(key, value); }
      finally { suppress = false; }
      save(); draw();
    };
    const send = message => frame.contentWindow?.postMessage({ v: 1, ...message }, origin);
    function flush() {
      commandTimer = 0;
      if (!connected || disposed || !Object.keys(pending).length) return;
      send({ type: 'chronozarr:set', ...pending }); pending = {};
      send({ type: 'chronozarr:get' }); // v1 has no separate playback/product acknowledgement.
    }
    function queue(key, value) {
      if (['product', 'band'].includes(key) && !value) return;
      pending[key] = value;
      if (key === 'band') {
        const limits = model.get('range') ?? null;
        bandRanges.set(activeBand, limits);
        const units = name => model.get('bands').find(item => item.name === name)?.units;
        pending.range = bandRanges.has(value) ? bandRanges.get(value) : units(value) === units(activeBand) ? limits : null;
        activeBand = value;
      } else if (key === 'product' && model.get('range')) pending.range = model.get('range');
      clearTimeout(commandTimer); commandTimer = setTimeout(flush, 0);
    }
    function draw() {
      const times = model.get('times');
      time.max = String(Math.max(0, times.length - 1)); time.value = String(model.get('t'));
      date.textContent = times[model.get('t')]?.slice(0, 10) ?? '';
      play.textContent = model.get('playing') ? 'Pause' : 'Play';
      product.value = model.get('product'); speed.value = String(model.get('speed'));
      band.value = model.get('band') ?? '';
      const limits = model.get('range');
      low.value = limits?.[0] ?? ''; high.value = limits?.[1] ?? '';
      const advanced = model.get('controls') === true;
      for (const control of [date, product, speed]) control.hidden = !advanced;
      const single = advanced && model.get('product') === 'band';
      for (const control of [band, low, high, auto, legend]) control.hidden = !single;
      const units = model.get('bands').find(item => item.name === model.get('band'))?.units ?? '';
      legend.textContent = limits ? `${limits[0]} → ${limits[1]} ${units}` : `${units} (auto limits)`;
      for (const control of [play, time, product, band, low, high, auto, speed]) control.disabled = !connected;
      frame.style.height = `${model.get('height')}px`;
    }
    function start() {
      connected = false; pending = {}; activeBand = ''; bandRanges.clear(); clearTimeout(commandTimer);
      update({ ready: false, error: {}, times: [], products: [], bands: [], state: {}, click: {} });
      status.textContent = 'Opening store…'; product.replaceChildren();
      const url = new URL(model.get('viewer_url'));
      origin = url.origin;
      if (!['http:', 'https:'].includes(location.protocol)) {
        status.textContent = 'This player needs a notebook served over HTTP or HTTPS.'; return;
      }
      url.searchParams.set('embed', '1'); url.searchParams.set('controls', '0');
      url.searchParams.set('origin', location.origin); url.searchParams.set('theme', model.get('theme'));
      url.searchParams.set('store', model.get('store_url')); url.searchParams.set('t', String(model.get('t')));
      if (model.get('product')) url.searchParams.set('p', model.get('product')); else url.searchParams.delete('p');
      frame.src = url.href; draw();
    }
    const receive = event => {
      if (disposed || event.origin !== origin || event.source !== frame.contentWindow) return;
      const message = event.data;
      if (!message || message.v !== 1) return;
      if (message.type === 'chronozarr:ready') {
        const first = !connected;
        connected = true; status.textContent = model.get('error').message ?? '';
        product.replaceChildren();
        for (const item of message.products.filter(item => item.available)) product.add(new Option(item.name, item.id));
        band.replaceChildren();
        for (const item of message.bands) band.add(new Option(`${item.name}${item.units ? ` (${item.units})` : ''}`, item.name));
        const state = message.state;
        activeBand = state.band;
        bandRanges.set(activeBand, state.range ?? null);
        const desired = first ? { playing: model.get('playing'), speed: model.get('speed'), band: model.get('band') ?? '', range: model.get('range') ?? null, ...pending } : null;
        update({ ready: true, times: message.times, products: message.products, bands: message.bands,
          state, t: state.t, product: state.product, band: state.band, range: state.range ?? null, playing: state.playing, speed: state.speed });
        if (desired) { pending = desired; flush(); }
      } else if (message.type === 'chronozarr:time') {
        update({ t: message.t, state: { ...model.get('state'), t: message.t, time: message.time } });
      } else if (message.type === 'chronozarr:view') {
        update({ state: { ...model.get('state'), zoom: message.zoom, center: message.center } });
      } else if (message.type === 'chronozarr:click') update({ click: message });
      else if (message.type === 'chronozarr:error') {
        update({ error: message }); status.textContent = message.message;
        if (connected) send({ type: 'chronozarr:get' });
      }
    };
    const listen = (key, callback) => { model.on(`change:${key}`, callback); subscriptions.push([`change:${key}`, callback]); };
    for (const key of ['t', 'product', 'band', 'range', 'playing', 'speed']) listen(key, () => { if (!suppress) { queue(key, model.get(key)); draw(); } });
    for (const key of ['store_url', 'viewer_url', 'theme']) listen(key, start);
    listen('height', draw);
    listen('controls', draw);
    const control = (key, value) => { model.set(key, value); save(); };
    play.onclick = () => control('playing', !model.get('playing'));
    time.oninput = () => control('t', Number(time.value));
    product.onchange = () => control('product', product.value);
    band.onchange = () => control('band', band.value);
    const applyLimits = () => {
      const limits = [Number(low.value), Number(high.value)];
      if (low.value !== '' && high.value !== '' && limits.every(Number.isFinite) && limits[0] < limits[1]) control('range', limits);
      else { status.textContent = 'Display limits must be finite and minimum < maximum.'; }
    };
    low.onchange = high.onchange = applyLimits; auto.onclick = () => control('range', null);
    speed.onchange = () => control('speed', Number(speed.value));
    const loaded = () => send({ type: 'chronozarr:get' });
    frame.addEventListener('load', loaded); window.addEventListener('message', receive); start();
    return () => {
      disposed = true; clearTimeout(saveTimer); clearTimeout(commandTimer);
      window.removeEventListener('message', receive); frame.removeEventListener('load', loaded);
      subscriptions.forEach(([event, callback]) => model.off(event, callback));
      frame.src = 'about:blank'; box.remove();
    };
  },
};
