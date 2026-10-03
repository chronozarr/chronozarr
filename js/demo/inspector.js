// Pure formatting of decoded pixel values for the inspector.
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
  return `
    <div class="sidebar-section">
      <div class="section-label">Location</div>
      <div class="meta-row"><span class="label">Time</span><span class="value">${timeLabel}</span></div>
      ${row('Pixel (x, y)', `${info.pixel.x}, ${info.pixel.y}`)}
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
