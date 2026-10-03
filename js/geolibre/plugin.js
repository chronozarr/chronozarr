import { ChronozarrLayer } from '../maplibre/layer.js';

/** GeoLibre plugin API adapter. Register in the app's plugin registry, not the Python iframe. */
export function createChronozarrPlugin({ url, name = 'chronozarr', id = 'chronozarr-geolibre', fitBounds = true, ...options }) {
  let app, map, layer, control, epoch = 0;
  let ready = Promise.resolve();
  const remove = () => {
    epoch++;
    if (control && map) map.removeControl(control);
    if (layer && map?.getLayer(id)) map.removeLayer(id);
    app?.unregisterExternalNativeLayer?.(id);
    app = map = layer = control = undefined;
  };
  return {
    id, name, version: '0.1.0', engines: ['maplibre'],
    get ready() { return ready; },
    activate(host) {
      if (layer) return true;
      map = host.getMap?.();
      if (!map) return false;
      app = host;
      const token = ++epoch;
      layer = new ChronozarrLayer({ ...options, id, url });
      const current = layer;
      map.addLayer(layer);
      control = {
        onAdd() {
          const box = document.createElement('div'); box.className = 'maplibregl-ctrl';
          box.style.cssText = 'background:white;padding:8px;font:13px system-ui';
          const title = document.createElement('span'); title.textContent = `${name}: opening…`;
          const slider = document.createElement('input'); slider.type = 'range'; slider.min = '0'; slider.disabled = true;
          slider.setAttribute('aria-label', `${name} timestep`);
          const label = () => { title.textContent = `${name}: ${current.times[current.t]?.slice(0, 10)}`; };
          slider.oninput = () => { current.setTime(Number(slider.value)); label(); };
          current.on('open', () => { slider.max = String(current.times.length - 1); slider.value = String(current.t); slider.disabled = false; label(); });
          current.on('error', event => { title.textContent = `${name}: ${event.error.message}`; });
          box.append(title, slider); this.box = box; return box;
        },
        onRemove() { this.box?.remove(); },
      };
      map.addControl(control, 'bottom-left');
      ready = current.opened.then(() => {
        if (token !== epoch) return;
        host.registerExternalNativeLayer?.({ id, name, nativeLayerIds: [id], paintMode: 'plugin',
          source: { type: 'chronozarr', url }, metadata: { bounds: current.bounds },
          paintBridge: { setOpacity: opacity => current.setOpacity(opacity) } });
        if (fitBounds) map.fitBounds(current.bounds, { padding: 40, duration: 0 });
      });
      // Consumers may await ready; handle failures for hosts whose activation contract is synchronous.
      ready.catch(() => {});
      return true;
    },
    deactivate: remove,
  };
}
