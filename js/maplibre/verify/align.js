// In-page helpers for checking where ChronozarrLayer draws (see ../README.md, "Checking alignment").
// Inject into the demo page (window.chronozarrDemo = {map, layer}) with truth.json from truth.py in hand:
//   settle(layer, map)                   resolves when the view is fully loaded and drawn
//   mask(a, b, outline, map, layer)      pixel by pixel: lit pixels outside the pyproj footprint (overdraw), and
//                                        unlit ones just inside it (nodata gap, or a misplaced edge?)
//   edges(a, b, outline, map, layer)     per scanline: position of the footprint edge vs the pyproj outline
//   grid(a, truth.levels[k], map)        positions of interior texel boundaries vs pyproj
// `a` and `b` are base64 PNG screenshots of the map with the layer at opacity 1 and 0 (`grid` takes `a` only).
// Errors are in CSS pixels; a perfectly placed edge reads within +-0.5 because pixels are sampled at their centres.
// Shared helpers injected into the page (as a string) by the harness scripts.
window.__align = {
  async settle(layer, map) {
    let stable = 0;
    const start = performance.now();
    while (stable < 4) {
      await new Promise((r) => setTimeout(r, 120));
      const s = layer.stats;
      if (s.state === 'ready' && s.pending === 0 && map.loaded()) stable++;
      else stable = 0;
      if (performance.now() - start > 90000) throw new Error('settle timeout ' + JSON.stringify(layer.stats));
    }
  },
  async decode(b64) {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
    const canvas = new OffscreenCanvas(bmp.width, bmp.height);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bmp, 0, 0);
    return ctx.getImageData(0, 0, bmp.width, bmp.height);
  },
  stats(errors) {
    if (errors.length === 0) return { n: 0 };
    const abs = errors.map(Math.abs).sort((a, b) => a - b);
    const mean = errors.reduce((a, b) => a + b, 0) / errors.length;
    return {
      n: errors.length,
      meanSigned: +mean.toFixed(3),
      maxAbs: +abs[abs.length - 1].toFixed(3),
      p99: +abs[Math.floor(abs.length * 0.99)].toFixed(3),
      within05: +(abs.filter((v) => v <= 0.5 + 1e-9).length / abs.length).toFixed(4),
    };
  },
  // Footprint edges: transitions of the on/off mask vs crossings of the predicted polygon.
  async edges(aB64, bB64, polygonLonLat, map, layer) {
    const a = await this.decode(aB64);
    const b = await this.decode(bB64);
    const { width: w, height: h } = a;
    const lit = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) {
      const d = Math.max(Math.abs(a.data[4 * i] - b.data[4 * i]), Math.abs(a.data[4 * i + 1] - b.data[4 * i + 1]), Math.abs(a.data[4 * i + 2] - b.data[4 * i + 2]));
      lit[i] = d > 10 ? 1 : 0;
    }
    const pts = polygonLonLat.map((ll) => map.project(ll));
    const horizontal = [];
    const vertical = [];
    const outliers = [];
    let unmatched = 0;
    const scan = async (axis) => {
      const length = axis === 'row' ? h : w;
      const span = axis === 'row' ? w : h;
      for (let line = 0; line < length; line += 2) {
        const c0 = line + 0.5;
        const crossings = [];
        for (let i = 0; i < pts.length; i++) {
          const p = pts[i];
          const q = pts[(i + 1) % pts.length];
          const pa = axis === 'row' ? p.y : p.x;
          const qa = axis === 'row' ? q.y : q.x;
          if ((pa - c0) * (qa - c0) < 0) {
            const f = (c0 - pa) / (qa - pa);
            crossings.push(axis === 'row' ? p.x + f * (q.x - p.x) : p.y + f * (q.y - p.y));
          }
        }
        crossings.sort((x, y) => x - y);
        if (crossings.length < 2) continue;
        const ends = [[crossings[0], 1], [crossings[crossings.length - 1], -1]]; // [position, direction of the on/off change]
        for (const [c, dir] of ends) {
          if (!(c > 3 && c < span - 3)) continue;
          let best = null;
          for (let s = Math.max(1, Math.round(c) - 4); s <= Math.min(span - 1, Math.round(c) + 4); s++) {
            const before = axis === 'row' ? lit[line * w + s - 1] : lit[(s - 1) * w + line];
            const after = axis === 'row' ? lit[line * w + s] : lit[s * w + line];
            if (after - before === dir && (best === null || Math.abs(s - c) < Math.abs(best - c))) best = s;
          }
          if (best === null) {
            unmatched++;
            continue;
          }
          const e = best - c;
          (axis === 'row' ? horizontal : vertical).push(e);
          if (Math.abs(e) > 0.5 + 1e-9) outliers.push({ axis, line, predicted: +c.toFixed(2), measured: best, e: +e.toFixed(2), dir });
        }
      }
    };
    await scan('row');
    await scan('col');
    // An outlier at a nodata texel is a real gap in the data (cloud mask), not a misplacement: check LOD 0 validity just inside the predicted edge.
    const classified = { nodataGap: 0, genuine: [] };
    for (const o of outliers.slice(0, 600)) {
      const inside = 0.3 * o.dir;
      const point = o.axis === 'row' ? [o.predicted + inside, o.line + 0.5] : [o.line + 0.5, o.predicted + inside];
      const value = await layer.getValueAt(map.unproject(point));
      if (value && !value.valid) classified.nodataGap++;
      else classified.genuine.push(o);
    }
    return { rowScan: this.stats(horizontal), colScan: this.stats(vertical), all: this.stats([...horizontal, ...vertical]), unmatched, outliers: outliers.length, classified: { nodataGap: classified.nodataGap, genuineCount: classified.genuine.length, genuineSample: classified.genuine.slice(0, 6) }, litFraction: +(lit.reduce((s, v) => s + v, 0) / lit.length).toFixed(3) };
  },
  // Interior texel boundaries: spikes of the column (row) derivative in a stripe vs the predicted boundary positions.
  async grid(aB64, truth, map) {
    const img = await this.decode(aB64);
    const { width: w, height: h } = img;
    const px = (x, y) => [img.data[4 * (y * w + x)], img.data[4 * (y * w + x) + 1], img.data[4 * (y * w + x) + 2]];
    const diff = (p, q) => Math.max(Math.abs(p[0] - q[0]), Math.abs(p[1] - q[1]), Math.abs(p[2] - q[2]));
    const result = {};
    for (const axis of ['cols', 'rows']) {
      const along = axis === 'cols' ? 'y' : 'x';
      const stripeCentre = axis === 'cols' ? Math.floor(h / 2) : Math.floor(w / 2);
      const length = axis === 'cols' ? w : h;
      const derivative = new Float64Array(length);
      for (let s = 1; s < length; s++) {
        let sum = 0;
        for (let o = -10; o < 10; o++) {
          const p0 = axis === 'cols' ? px(s - 1, stripeCentre + o) : px(stripeCentre + o, s - 1);
          const p1 = axis === 'cols' ? px(s, stripeCentre + o) : px(stripeCentre + o, s);
          sum += diff(p0, p1);
        }
        derivative[s] = sum / 20;
      }
      const predicted = [];
      for (const line of truth[axis]) {
        const pa = map.project(line.a);
        const pb = map.project(line.b);
        const [a0, a1, b0, b1] = axis === 'cols' ? [pa.y, pa.x, pb.y, pb.x] : [pa.x, pa.y, pb.x, pb.y];
        const p = a1 + ((b1 - a1) * (stripeCentre + 0.5 - a0)) / (b0 - a0);
        if (p > 3 && p < length - 3) predicted.push(p);
      }
      predicted.sort((x, y) => x - y);
      const spikes = [];
      for (let s = 1; s < length; s++) if (derivative[s] > 1 && s >= predicted[0] && s <= predicted[predicted.length - 1]) spikes.push(s);
      const errors = [];
      let orphans = 0;
      for (const s of spikes) {
        let nearest = predicted[0];
        for (const p of predicted) if (Math.abs(p - s) < Math.abs(nearest - s)) nearest = p;
        const e = s - nearest;
        if (Math.abs(e) > 0.51) orphans++;
        errors.push(e);
      }
      result[axis] = { predicted: predicted.length, spikes: spikes.length, orphans, ...this.stats(errors), spacingPx: +((predicted[predicted.length - 1] - predicted[0]) / (predicted.length - 1)).toFixed(2) };
      void along;
    }
    return result;
  },
};

// Footprint mask vs the predicted polygon, pixel by pixel (valid for any bearing and pitch).
window.__align.mask = async function (aB64, bB64, polygonLonLat, map, layer) {
  const a = await this.decode(aB64);
  const b = await this.decode(bB64);
  const { width: w, height: h } = a;
  const pts = polygonLonLat.map((ll) => map.project(ll));
  const n = pts.length;
  const distanceToEdge = (x, y) => {
    let best = Infinity;
    for (let i = 0; i < n; i++) {
      const p = pts[i];
      const q = pts[(i + 1) % n];
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      const len2 = dx * dx + dy * dy;
      const f = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((x - p.x) * dx + (y - p.y) * dy) / len2));
      best = Math.min(best, Math.hypot(x - (p.x + f * dx), y - (p.y + f * dy)));
    }
    return best;
  };
  let lit = 0;
  let inside = 0;
  const overdraw = [];
  const underdraw = [];
  for (let y = 0; y < h; y++) {
    const yc = y + 0.5;
    const crossings = [];
    for (let i = 0; i < n; i++) {
      const p = pts[i];
      const q = pts[(i + 1) % n];
      if ((p.y - yc) * (q.y - yc) < 0) crossings.push(p.x + ((yc - p.y) / (q.y - p.y)) * (q.x - p.x));
    }
    crossings.sort((u, v) => u - v);
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      const d = Math.max(Math.abs(a.data[4 * i] - b.data[4 * i]), Math.abs(a.data[4 * i + 1] - b.data[4 * i + 1]), Math.abs(a.data[4 * i + 2] - b.data[4 * i + 2]));
      const isLit = d > 10;
      const xc = x + 0.5;
      let count = 0;
      for (const c of crossings) if (c < xc) count++;
      const isInside = count % 2 === 1;
      if (isLit) lit++;
      if (isInside) inside++;
      if (isLit && !isInside) overdraw.push([x, y]);
      else if (!isLit && isInside && crossings.length > 0) underdraw.push([x, y]);
    }
  }
  const worstOver = overdraw.reduce((m, [x, y]) => Math.max(m, distanceToEdge(x + 0.5, y + 0.5)), 0);
  // Unlit pixels inside the footprint within 8 px of its edge: nodata gaps (clouds) or a misplaced edge?
  const near = underdraw.filter(([x, y]) => distanceToEdge(x + 0.5, y + 0.5) < 8);
  let nodata = 0;
  const valid = [];
  const stride = Math.max(1, Math.ceil(near.length / 400));
  for (let k = 0; k < near.length; k += stride) {
    const [x, y] = near[k];
    const v = await layer.getValueAt(map.unproject([x + 0.5, y + 0.5]));
    if (v && !v.valid) nodata++;
    else valid.push({ x, y, d: +distanceToEdge(x + 0.5, y + 0.5).toFixed(2) });
  }
  return { lit, inside, overdrawPixels: overdraw.length, maxOverdrawPx: +worstOver.toFixed(3), underdrawNearEdge: near.length, checked: Math.ceil(near.length / stride), nodataAtLod0: nodata, validAtLod0: valid.length, validSample: valid.slice(0, 5), maxValidDepth: valid.reduce((m, v) => Math.max(m, v.d), 0) };
};
