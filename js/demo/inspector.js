// Pure formatting of decoded pixel values for the inspector.
import { createProjection, pixelToCrs } from '../maplibre/projection.js';

/** WGS84 coordinates at the center of the selected level-0 pixel. */
export function pixelLocation(pixel, store) {
  if (!store?.transform || !store.crs) return null;
  try {
    const [lon, lat] = createProjection(store.crs).toLonLat(...pixelToCrs(store.transform, pixel.x + 0.5, pixel.y + 0.5));
    return Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon } : null;
  } catch {
    return null;
  }
}
const ndviColor = (v) => (v === null ? 'var(--text-3)' : v > 0.3 ? 'var(--green)' : v > 0 ? 'var(--amber)' : 'var(--red)');
const ndwiColor = (v) => (v === null ? 'var(--text-3)' : v > 0 ? 'var(--accent)' : 'var(--text-2)');

function metricHtml(name, value, color) {
  const frac = value === null ? 0 : ((value + 1) / 2) * 100;
  return `
    <div class="metric">
      <div class="metric-header">
        <span class="metric-name">${name}</span>
        <span class="metric-value" style="color:${color}">${value === null ? '—' : value.toFixed(3)}</span>
      </div>
      <div class="metric-bar"><div class="metric-fill" style="width:${frac}%;background:${color}"></div></div>
    </div>`;
}

/** A value for display: whole numbers as they are, others with 3 significant-ish digits (more decimals for small values). */
export function formatValue(value) {
  if (!Number.isFinite(value) || Number.isInteger(value)) return String(value);
  const magnitude = Math.abs(value);
  return value.toFixed(magnitude >= 1000 ? 1 : magnitude >= 10 ? 2 : 3);
}

export function sidebarHtml(info, timeLabel, store) {
  const indices = [];
  if (info.hasNdvi) indices.push(metricHtml('NDVI', info.ndvi, ndviColor(info.ndvi)));
  if (info.hasNdwi) {
    indices.push(metricHtml('NDWI', info.ndwi, ndwiColor(info.ndwi)));
    indices.push(`
      <div class="metric">
        <div class="metric-header">
          <span class="metric-name">Water</span>
          <span class="water-badge ${info.isWater ? 'yes' : 'no'}">
            <span class="water-dot" style="background:${info.isWater ? 'var(--water)' : 'var(--text-3)'}"></span>
            ${info.isWater ? 'Detected' : 'None'}
          </span>
        </div>
      </div>`);
  }
  const row = (label, value) => `<div class="meta-row"><span class="label">${label}</span><span class="value mono">${value}</span></div>`;
  const reflectance = info.bands.every((b) => b.reflectance);
  const physical = (b) => (reflectance ? b.value.toFixed(4) : `${formatValue(b.value)}${b.units && b.units !== 'reflectance' ? ` ${b.units}` : ''}`);
  const noData = '<span class="no-data">no data</span>';
  const scaled = info.valid && info.bands.some((b) => b.value !== b.stored);
  const status = info.valid ? '' : row('Status', `<span class="no-data">No data</span> · ${info.masked ? 'masked out' : 'at the nodata value'}`);
  const observed = info.observed === null || info.observed === undefined ? '' : row('Observed by', info.observed === 0 ? 'no scene (gap-filled)' : `${info.observed} scene${info.observed === 1 ? '' : 's'}`);
  const location = pixelLocation(info.pixel, store);
  const tuple = location ? `(${location.lat.toFixed(6)}, ${location.lon.toFixed(6)})` : null;
  return `
    <div class="sidebar-section">
      <div class="section-label">Location</div>
      <div class="meta-row"><span class="label">Time</span><span class="value">${timeLabel}</span></div>
      <div class="coordinate-location">
        <div class="coordinate-heading"><span>Latitude, longitude</span>${tuple ? '<button type="button" id="copy-coordinates" aria-label="Copy latitude and longitude"><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5V3A1.5 1.5 0 0 0 9 1.5H3A1.5 1.5 0 0 0 1.5 3v6A1.5 1.5 0 0 0 3 10.5h.5"/></svg><span>Copy</span></button>' : ''}</div>
        <div class="mono coordinate-tuple" id="coordinate-tuple">${tuple ?? 'Unavailable'}</div>
      </div>
      <div id="coordinate-copy-status" class="coordinate-copy-status" role="status"></div>
      ${row('Level', info.lod === 0 ? '0 (full resolution)' : `${info.lod} (${2 ** info.lod}× coarser)`)}
      ${status}
      ${observed}
    </div>
    ${indices.length ? `<div class="sidebar-section"><div class="section-label">Indices</div>${indices.join('')}</div>` : ''}
    <div class="sidebar-section">
      <div class="section-label">Over time</div>
      <div id="chart-legend" class="chart-legend"></div>
      <div id="chart" class="chart"></div>
      <div id="chart-status" class="chart-status"></div>
    </div>
    <div class="sidebar-section">
      <div class="section-label">${reflectance ? 'Reflectance' : 'Value'}</div>
      ${info.bands.map((b) => row(b.name, info.valid ? physical(b) : noData)).join('')}
    </div>
    ${
      scaled
        ? `<div class="sidebar-section">
      <div class="section-label">Stored value</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;">
        ${info.bands.map((b) => row(b.name, b.stored)).join('')}
      </div>
    </div>`
        : ''
    }`;
}
