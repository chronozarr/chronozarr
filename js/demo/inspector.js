// Pure formatting of decoded pixel values for the inspector.

/** Escape store metadata before putting it into an HTML template. */
export function escapeHtml(text) {
  return String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

/** WGS84 coordinates at the center of the selected level-0 pixel. */
export async function pixelLocation(pixel, store) {
  if (!store?.transform) return null;
  try {
    // Reuse the lazy projection adapter, keeping it off the initial-view load path.
    const { createProjection, pixelToCrs } = await import('../maplibre/projection.js');
    const projection = await createProjection(store);
    const [lon, lat] = projection.toLonLat(...pixelToCrs(store.transform, pixel.x + 0.5, pixel.y + 0.5));
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90) return null;
    return { lat, lon: ((lon + 180) % 360 + 360) % 360 - 180 };
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

export function sidebarHtml(info, timeLabel) {
  const indices = [];
  if (info.hasNdvi) indices.push(metricHtml('NDVI', info.ndvi, ndviColor(info.ndvi)));
  if (info.hasNdwi) indices.push(metricHtml('NDWI', info.ndwi, ndwiColor(info.ndwi)));
  if (info.ndwi !== null) {
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
  const physical = (b) => (reflectance ? b.value.toFixed(4) : `${formatValue(b.value)}${b.units && b.units !== 'reflectance' ? ` ${escapeHtml(b.units)}` : ''}`);
  const noData = '<span class="no-data">no data</span>';
  const scaled = info.valid && info.bands.some((b) => b.value !== b.stored);
  const status = info.valid ? '' : row('Status', `<span class="no-data">No data</span> · ${info.masked ? 'masked out' : 'at the nodata value'}`);
  const observed = info.observed === null || info.observed === undefined ? '' : row('Observed by', info.observed === 0 ? 'no scene (gap-filled)' : `${info.observed} scene${info.observed === 1 ? '' : 's'}`);
  return `
    <div class="sidebar-section">
      <div class="section-label">Location</div>
      <div class="meta-row"><span class="label">Snapshot time</span><span class="value">${timeLabel}</span></div>
      ${row('Pixel (x, y)', `${info.pixel.x}, ${info.pixel.y}`)}
      <div class="coordinate-location">
        <div class="coordinate-heading"><span>Latitude, longitude</span><button type="button" id="copy-coordinates" aria-label="Copy latitude and longitude" hidden>Copy</button></div>
        <div class="mono coordinate-tuple" id="coordinate-tuple">Loading coordinates…</div>
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
      ${info.bands.map((b) => row(escapeHtml(b.name), b.value === null ? noData : physical(b))).join('')}
    </div>
    ${
      scaled
        ? `<div class="sidebar-section">
      <div class="section-label">Stored value</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;">
        ${info.bands.map((b) => row(escapeHtml(b.name), b.stored ?? noData)).join('')}
      </div>
    </div>`
        : ''
    }`;
}
