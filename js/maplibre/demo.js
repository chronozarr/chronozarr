// Demo: the live Ucayali store as a MapLibre custom layer over MapLibre's demo tiles.
//   ?store=<url>  open another chronozarr store (default: the Ucayali store on data.chronozarr.org)

import * as maplibregl from 'maplibre-gl';
import { makeTimeFormatter } from '../shared/products.js';
import { ChronozarrLayer } from './layer.js';

const DEFAULT_STORE = 'https://data.chronozarr.org/ucayali_santa_maria_v03';
const params = new URLSearchParams(location.search);
const storeUrl = params.get('store') ?? DEFAULT_STORE;
const $ = (id) => document.getElementById(id);

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://demotiles.maplibre.org/style.json',
  center: [-75, -7.7],
  zoom: 9,
  hash: false,
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
map.addControl(new maplibregl.ScaleControl(), 'bottom-right');

const layer = new ChronozarrLayer({ url: storeUrl, product: params.get('p') ?? 'true_color', t: 0, prefetch: true });
window.chronozarrDemo = { map, layer };

function showError(error) {
  $('error').textContent = error instanceof Error ? error.message : String(error);
}

layer.on('loading', () => setStatus('loading', 'loading'));
layer.on('ready', () => setStatus('ready', 'ready'));
layer.on('error', (event) => showError(event.error));

function setStatus(state, text) {
  $('status').dataset.state = state;
  $('status').textContent = text;
}

layer.on('open', () => {
  const formatTime = makeTimeFormatter(layer.times);
  const slider = $('time');
  slider.max = String(layer.times.length - 1);
  slider.disabled = false;
  const showTime = () => {
    $('time-label').textContent = formatTime(layer.t);
    $('time-index').textContent = `${layer.t + 1} / ${layer.times.length}`;
  };
  slider.addEventListener('input', () => {
    layer.setTime(Number(slider.value));
    showTime();
  });
  showTime();

  const buttons = $('products');
  for (const product of layer.products.filter((p) => p.available && p.id !== 'band')) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = product.name;
    button.dataset.product = product.id;
    button.setAttribute('aria-pressed', String(product.id === layer.product));
    button.addEventListener('click', () => {
      layer.setProduct(product.id);
      for (const other of buttons.children) other.setAttribute('aria-pressed', String(other === button));
    });
    buttons.append(button);
  }
  map.fitBounds(layer.bounds, { padding: 80, duration: 0 });
});

$('opacity').addEventListener('input', (event) => layer.setOpacity(Number(event.target.value)));

map.on('load', () => {
  map.addLayer(layer, map.getLayer('countries-label') ? 'countries-label' : undefined);
});

setInterval(() => {
  const store = layer.store;
  if (!store) return;
  const { requests, bytes } = store.stats.network;
  const { lod, slots, residentSlots, pending } = layer.stats;
  $('stats').textContent = `zoom ${map.getZoom().toFixed(2)} · level ${lod} · ${requests} requests · ${(bytes / 1e6).toFixed(1)} MB · gpu ${residentSlots}/${slots} · pending ${pending}`;
}, 300);

let marker = null;
map.on('click', async (event) => {
  let value;
  try {
    value = await layer.getValueAt(event.lngLat);
  } catch (error) {
    showError(error);
    return;
  }
  const readout = $('readout');
  readout.replaceChildren();
  if (!value) {
    readout.textContent = 'Outside the store.';
    marker?.remove();
    return;
  }
  showError('');
  marker ??= new maplibregl.Marker({ color: '#3b82f6', scale: 0.6 });
  marker.setLngLat([value.lngLat.lng, value.lngLat.lat]).addTo(map);

  const table = document.createElement('table');
  const addRow = (label, text) => {
    const row = table.insertRow();
    const head = row.insertCell();
    head.textContent = label;
    head.className = 'muted';
    const cell = row.insertCell();
    cell.textContent = text;
    cell.className = 'num mono';
  };
  addRow('time', value.time.slice(0, 10));
  addRow('pixel', `col ${value.col}, row ${value.row}`);
  addRow('centre', `${value.lngLat.lng.toFixed(5)}, ${value.lngLat.lat.toFixed(5)}`);
  if (!value.valid) addRow('data', 'no observation');
  for (const band of value.bands) addRow(band.name, `${band.stored}  (${band.value.toFixed(4)}${band.units ? ` ${band.units}` : ''})`);
  if (value.ndvi !== null) addRow('NDVI', value.ndvi.toFixed(3));
  if (value.ndwi !== null) addRow('NDWI', `${value.ndwi.toFixed(3)}${value.isWater ? ' (water)' : ''}`);
  readout.append(table);
});
