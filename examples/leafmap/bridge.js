// Adapt py-maplibregl's JSON calls to actual MapLibre custom-layer instances.
// The upstream renderer owns the map; its model interface remains unchanged.
export async function renderBridge({ model, el }, source, readerUrl) {
  const { ChronozarrLayer } = await import(readerUrl);
  const blob = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
  let upstream;
  try { upstream = (await import(blob)).default; }
  finally { URL.revokeObjectURL(blob); }
  const layers = new Map();
  const controls = document.createElement('div');
  controls.style.cssText = 'display:grid;gap:8px;padding:8px;font:13px system-ui';
  el.append(controls);
  function convert(calls) {
    return calls.map(([method, args]) => {
      const descriptor = args[0];
      if (method !== 'addLayer' || descriptor?.type !== 'chronozarr-notebook') return [method, args];
      const { options, fitBounds } = descriptor;
      const layer = new ChronozarrLayer(options);
      layers.set(layer.id, layer);
      const row = document.createElement('label');
      const title = document.createElement('span');
      const slider = document.createElement('input');
      slider.type = 'range'; slider.min = '0'; slider.value = String(options.t ?? 0);
      slider.disabled = true; slider.setAttribute('aria-label', `${layer.id} timestep`);
      slider.style.width = '100%';
      title.textContent = `${layer.id}: opening…`;
      row.append(title, slider); controls.append(row);
      const label = () => { title.textContent = `${layer.id}: ${new Date(layer.times[layer.t]).toISOString().slice(0, 10)}`; };
      slider.addEventListener('input', () => { layer.setTime(Number(slider.value)); label(); });
      layer.on('open', () => {
        slider.max = String(layer.times.length - 1); slider.disabled = false; label();
      });
      layer.on('error', event => { title.textContent = `${layer.id}: ${event.error.message}`; });
      if (fitBounds) {
        const onAdd = layer.onAdd.bind(layer);
        layer.onAdd = (map, gl) => {
          onAdd(map, gl);
          layer.opened.then(() => map.fitBounds(layer.bounds, { padding: 40, duration: 0 })).catch(() => {});
        };
      }
      return [method, [layer, ...args.slice(1)]];
    });
  }
  const callbacks = [];
  const proxy = new Proxy(model, {
    get(target, key) {
      if (key === 'get') return name => name === 'calls' ? convert(target.get(name)) : target.get(name);
      if (key === 'on') return (event, callback, ...args) => {
        if (event !== 'msg:custom') return target.on(event, callback, ...args);
        const wrapped = message => callback({ ...message, calls: convert(message.calls ?? []) });
        callbacks.push([event, wrapped]);
        return target.on(event, wrapped, ...args);
      };
      const value = Reflect.get(target, key);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  const cleanup = await upstream.render({ model: proxy, el });
  return () => {
    callbacks.forEach(([event, callback]) => model.off(event, callback));
    layers.forEach(layer => layer.onRemove());
    cleanup?.(); controls.remove();
  };
}
