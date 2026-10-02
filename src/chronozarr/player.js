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
    const speed = document.createElement('select'); speed.setAttribute('aria-label', 'Playback speed');
    for (const value of [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,20,24,30,40,48,60]) {
      speed.add(new Option(`${value}/s`, String(value)));
    }
    const status = document.createElement('div'); status.setAttribute('role', 'status');
    const frame = document.createElement('iframe'); frame.title = 'chronozarr player';
    frame.style.cssText = 'width:100%;border:0;min-height:320px';
    frame.allow = 'fullscreen; local-network-access';
    controls.append(play, time, date, product, speed); box.append(controls, frame, status); el.append(box);
    let origin, connected = false, disposed = false, suppress = false, saveTimer = 0, commandTimer = 0;
    let pending = {};
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
      send({ type: 'tileripper:set', ...pending }); pending = {};
      send({ type: 'tileripper:get' }); // v1 has no separate playback/product acknowledgement.
    }
    function queue(key, value) {
      if (key === 'product' && !value) return;
      pending[key] = value;
      clearTimeout(commandTimer); commandTimer = setTimeout(flush, 0);
    }
    function draw() {
      const times = model.get('times');
      time.max = String(Math.max(0, times.length - 1)); time.value = String(model.get('t'));
      date.textContent = times[model.get('t')]?.slice(0, 10) ?? '';
      play.textContent = model.get('playing') ? 'Pause' : 'Play';
      product.value = model.get('product'); speed.value = String(model.get('speed'));
      for (const control of [play, time, product, speed]) control.disabled = !connected;
      frame.style.height = `${model.get('height')}px`;
    }
    function start() {
      connected = false; pending = {}; clearTimeout(commandTimer);
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
      if (message.type === 'tileripper:ready') {
        const first = !connected;
        connected = true; status.textContent = model.get('error').message ?? '';
        product.replaceChildren();
        for (const item of message.products.filter(item => item.available)) product.add(new Option(item.name, item.id));
        const state = message.state;
        const desired = first ? { playing: model.get('playing'), speed: model.get('speed'), ...pending } : null;
        update({ ready: true, times: message.times, products: message.products, bands: message.bands,
          state, t: state.t, product: state.product, playing: state.playing, speed: state.speed });
        if (desired) { pending = desired; flush(); }
      } else if (message.type === 'tileripper:time') {
        update({ t: message.t, state: { ...model.get('state'), t: message.t, time: message.time } });
      } else if (message.type === 'tileripper:view') {
        update({ state: { ...model.get('state'), zoom: message.zoom, center: message.center } });
      } else if (message.type === 'tileripper:click') update({ click: message });
      else if (message.type === 'tileripper:error') {
        update({ error: message }); status.textContent = message.message;
        if (connected) send({ type: 'tileripper:get' });
      }
    };
    const listen = (key, callback) => { model.on(`change:${key}`, callback); subscriptions.push([`change:${key}`, callback]); };
    for (const key of ['t', 'product', 'playing', 'speed']) listen(key, () => { if (!suppress) { queue(key, model.get(key)); draw(); } });
    for (const key of ['store_url', 'viewer_url', 'theme']) listen(key, start);
    listen('height', draw);
    const control = (key, value) => { model.set(key, value); save(); };
    play.onclick = () => control('playing', !model.get('playing'));
    time.oninput = () => control('t', Number(time.value));
    product.onchange = () => control('product', product.value);
    speed.onchange = () => control('speed', Number(speed.value));
    const loaded = () => send({ type: 'tileripper:get' });
    frame.addEventListener('load', loaded); window.addEventListener('message', receive); start();
    return () => {
      disposed = true; clearTimeout(saveTimer); clearTimeout(commandTimer);
      window.removeEventListener('message', receive); frame.removeEventListener('load', loaded);
      subscriptions.forEach(([event, callback]) => model.off(event, callback));
      frame.src = 'about:blank'; box.remove();
    };
  },
};
