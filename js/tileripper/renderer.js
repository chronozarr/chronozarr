// WebGL2 renderer for chronozarr cells.
//
// All chunks live as raw uint16 in one R16UI TEXTURE_2D_ARRAY. A chunk (band, y, x) occupies
// n_band consecutive layers ("slot"), so uploading one is a single texSubImage3D from the decoded
// array. The fragment shader reads the anchor slot and, for delta timesteps, the delta slot and
// adds them per pixel, so switching timesteps or products never runs a CPU loop over pixels.

import { GAIN, REFLECTANCE_SCALE } from './products.js';

const BACKGROUND = [0.035, 0.047, 0.071];

const VERTEX_SHADER = `#version 300 es
uniform vec2 u_canvas;
uniform vec3 u_view;        // center x, center y (world px), scale (canvas px per world px)
uniform vec4 u_cell;        // world origin x, y and world size x, y
uniform vec2 u_extent;      // valid texels in the cell
out vec2 v_texel;
void main() {
  vec2 corner = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));
  vec2 world = u_cell.xy + corner * u_cell.zw;
  vec2 px = (world - u_view.xy) * u_view.z + 0.5 * u_canvas;
  gl_Position = vec4(px.x / u_canvas.x * 2.0 - 1.0, 1.0 - px.y / u_canvas.y * 2.0, 0.0, 1.0);
  v_texel = corner * u_extent;
}`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp int;
precision highp usampler2DArray;
in vec2 v_texel;
uniform usampler2DArray u_data;
uniform vec2 u_extent;
uniform int u_anchorBase;   // first layer of the anchor slot
uniform int u_deltaBase;    // first layer of the delta slot; -1 when the timestep is itself an anchor
uniform ivec3 u_inputs;     // band index per product input, -1 = unused
uniform int u_product;
uniform float u_stretch_lo;
uniform int u_nodata;
out vec4 outColor;

const float SCALE = ${REFLECTANCE_SCALE.toFixed(1)};
const float GAIN = ${GAIN.toFixed(1)};
const float SAT_BOOST = 1.4;
const vec3 BG = vec3(${BACKGROUND.join(', ')});

// star-delta: value = clamp(anchor + int16(delta), 0, 65535)
int value(ivec2 texel, int band) {
  if (band < 0) return 0;
  int a = int(texelFetch(u_data, ivec3(texel, u_anchorBase + band), 0).r);
  if (u_deltaBase < 0) return a;
  int d = int(texelFetch(u_data, ivec3(texel, u_deltaBase + band), 0).r);
  if (d >= 32768) d -= 65536;
  return clamp(a + d, 0, 65535);
}

float toSrgb(float v) {
  return v <= 0.0031308 ? 12.92 * v : 1.055 * pow(v, 1.0 / 2.4) - 0.055;
}

vec3 tone(vec3 refl, float lo) {
  vec3 sc = refl * GAIN;
  vec3 tm = sc / (sc + 1.0);
  vec3 sr = vec3(toSrgb(tm.r), toSrgb(tm.g), toSrgb(tm.b));
  float headroom = 1.0 - lo;
  if (headroom > 0.001) sr = clamp((sr - lo) / headroom, 0.0, 1.0);
  return sr;
}

vec3 trueColor(vec3 rgb, float lo) {
  vec3 sr = tone(rgb, lo);
  float lum = dot(sr, vec3(0.2126, 0.7152, 0.0722));
  return clamp(mix(vec3(lum), sr, SAT_BOOST), 0.0, 1.0);
}

vec3 ndviRamp(float v) {
  if (v < 0.0) return mix(vec3(0.14, 0.15, 0.19), vec3(0.48, 0.42, 0.36), clamp(v + 1.0, 0.0, 1.0));
  vec3 c0 = vec3(0.76, 0.70, 0.52);
  vec3 c1 = vec3(0.50, 0.76, 0.32);
  vec3 c2 = vec3(0.18, 0.52, 0.16);
  vec3 c3 = vec3(0.04, 0.28, 0.04);
  if (v < 0.2) return mix(c0, c1, v / 0.2);
  if (v < 0.5) return mix(c1, c2, (v - 0.2) / 0.3);
  return mix(c2, c3, clamp((v - 0.5) / 0.5, 0.0, 1.0));
}

vec3 ndwiRamp(float v) {
  if (v < 0.0) return mix(vec3(0.32, 0.26, 0.20), vec3(0.58, 0.52, 0.42), clamp(v + 1.0, 0.0, 1.0));
  vec3 c0 = vec3(0.62, 0.80, 0.94);
  vec3 c1 = vec3(0.22, 0.52, 0.84);
  vec3 c2 = vec3(0.06, 0.22, 0.52);
  if (v < 0.3) return mix(c0, c1, v / 0.3);
  return mix(c1, c2, clamp((v - 0.3) / 0.7, 0.0, 1.0));
}

void main() {
  ivec2 texel = ivec2(min(floor(v_texel), u_extent - 1.0));
  int v0 = value(texel, u_inputs.x);
  int v1 = value(texel, u_inputs.y);
  int v2 = value(texel, u_inputs.z);
  bool empty = (u_inputs.x < 0 || v0 == u_nodata) && (u_inputs.y < 0 || v1 == u_nodata) && (u_inputs.z < 0 || v2 == u_nodata);
  if (empty) {
    outColor = vec4(BG, 1.0);
    return;
  }
  vec3 x = vec3(float(v0), float(v1), float(v2)) / SCALE;

  if (u_product == 0) {
    outColor = vec4(trueColor(x, u_stretch_lo), 1.0);            // inputs: red, green, blue
  } else if (u_product == 1) {
    outColor = vec4(tone(x, u_stretch_lo), 1.0);                 // inputs: nir, red, green -> R, G, B
  } else if (u_product == 2) {
    float nir = x.x, red = x.y;                                  // inputs: nir, red
    outColor = vec4(ndviRamp((nir + red) > 0.0 ? (nir - red) / (nir + red) : 0.0), 1.0);
  } else if (u_product == 3) {
    float green = x.x, nir = x.y;                                // inputs: green, nir
    outColor = vec4(ndwiRamp((green + nir) > 0.0 ? (green - nir) / (green + nir) : 0.0), 1.0);
  } else if (u_product == 4) {
    float green = x.x, nir = x.y;
    float ndwi = (green + nir) > 0.0 ? (green - nir) / (green + nir) : 0.0;
    if (ndwi > 0.0) {
      outColor = vec4(0.18, 0.48, 0.84, 1.0);
    } else {
      float lum = tone(vec3(green), u_stretch_lo).r;
      outColor = vec4(vec3(lum * 0.65 + 0.06), 1.0);
    }
  } else {
    outColor = vec4(vec3(tone(vec3(x.x), u_stretch_lo).r), 1.0); // single band, grayscale
  }
}`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`Shader compile failed: ${log}`);
  }
  return shader;
}

export class Renderer {
  #gl;
  #uniforms = {};
  #texture = null;
  #pool = null;
  #frame = 0;

  /** Called with a slot's cell metadata; the highest score is evicted first. Set by the viewer. */
  evictionScore = () => 0;
  stats = { uploads: 0, uploadMs: 0, evictions: 0 };

  constructor(canvas) {
    const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, preserveDrawingBuffer: true });
    if (!gl) throw new Error('WebGL2 is not available in this browser.');
    this.#gl = gl;
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 2);
    const program = gl.createProgram();
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`Program link failed: ${gl.getProgramInfoLog(program)}`);
    }
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    gl.useProgram(program);
    for (const name of ['u_canvas', 'u_view', 'u_cell', 'u_extent', 'u_data', 'u_anchorBase', 'u_deltaBase', 'u_inputs', 'u_product', 'u_stretch_lo', 'u_nodata']) {
      this.#uniforms[name] = gl.getUniformLocation(program, name);
    }
    gl.uniform1i(this.#uniforms.u_data, 0);
    this.limits = { maxLayers: gl.getParameter(gl.MAX_ARRAY_TEXTURE_LAYERS), maxSize: gl.getParameter(gl.MAX_TEXTURE_SIZE) };
  }

  get slots() {
    return this.#pool?.slots ?? 0;
  }

  /** Number of slots that fit `budgetBytes` and the driver's layer limit. */
  planSlots(nBand, chunkWidth, chunkHeight, budgetBytes, wanted) {
    const byLayers = Math.floor(this.limits.maxLayers / nBand);
    const byBytes = Math.floor(budgetBytes / (nBand * chunkWidth * chunkHeight * 2));
    return Math.max(0, Math.min(wanted, byLayers, byBytes));
  }

  /** (Re)allocate the texture pool. Drops every resident chunk. */
  configure({ nBand, chunkWidth, chunkHeight, slots }) {
    const gl = this.#gl;
    if (chunkWidth > this.limits.maxSize || chunkHeight > this.limits.maxSize) {
      throw new Error(`Chunk ${chunkWidth}x${chunkHeight} exceeds MAX_TEXTURE_SIZE ${this.limits.maxSize}.`);
    }
    if (slots < 2) throw new Error(`Texture pool has ${slots} slots; at least 2 are needed (anchor + delta).`);
    if (this.#texture) gl.deleteTexture(this.#texture);
    this.#texture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.#texture);
    gl.texStorage3D(gl.TEXTURE_2D_ARRAY, 1, gl.R16UI, chunkWidth, chunkHeight, slots * nBand);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.#pool = {
      nBand,
      chunkWidth,
      chunkHeight,
      slots,
      meta: new Array(slots).fill(null),
      keyToSlot: new Map(),
      usedInFrame: new Uint32Array(slots),
      free: Array.from({ length: slots }, (_, i) => slots - 1 - i),
    };
    this.resetStats();
  }

  resetStats() {
    Object.assign(this.stats, { uploads: 0, uploadMs: 0, evictions: 0 });
  }

  /** Forget every resident chunk (keeps the texture allocation). */
  clearResident() {
    const pool = this.#pool;
    pool.keyToSlot.clear();
    pool.meta.fill(null);
    pool.usedInFrame.fill(0);
    pool.free = Array.from({ length: pool.slots }, (_, i) => pool.slots - 1 - i);
  }

  /** Start a frame: slots touched from now on are protected from eviction until the next frame. */
  newFrame() {
    this.#frame++;
  }

  /** Resident slot for `key`, or -1. Marks it in use for this frame. */
  slotOf(key) {
    const slot = this.#pool.keyToSlot.get(key);
    if (slot === undefined) return -1;
    this.#pool.usedInFrame[slot] = this.#frame;
    return slot;
  }

  isResident(key) {
    return this.#pool.keyToSlot.has(key);
  }

  /**
   * Upload a decoded chunk (uint16, [band][y][x]) into a slot and return the slot, or -1 when no
   * slot can be freed. `meta` carries {lod,row,col,t} for eviction scoring. Foreground uploads
   * (needed for the frame being drawn) evict the highest-scoring unprotected slot; `background`
   * uploads only take a free slot or replace a slot that scores worse than the incoming chunk.
   */
  upload(key, meta, data, { background = false } = {}) {
    const pool = this.#pool;
    const existing = this.slotOf(key);
    if (existing >= 0) return existing;
    let slot = pool.free.pop();
    if (slot === undefined) {
      let victim = -1;
      let victimScore = -Infinity;
      for (let s = 0; s < pool.slots; s++) {
        if (pool.usedInFrame[s] === this.#frame) continue;
        const score = this.evictionScore(pool.meta[s]);
        if (score > victimScore) {
          victim = s;
          victimScore = score;
        }
      }
      if (victim < 0) return -1;
      if (background && victimScore <= this.evictionScore(meta)) return -1;
      pool.keyToSlot.delete(pool.meta[victim].key);
      this.stats.evictions++;
      slot = victim;
    }
    const gl = this.#gl;
    const started = performance.now();
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.#texture);
    gl.texSubImage3D(gl.TEXTURE_2D_ARRAY, 0, 0, 0, slot * pool.nBand, pool.chunkWidth, pool.chunkHeight, pool.nBand, gl.RED_INTEGER, gl.UNSIGNED_SHORT, data);
    this.stats.uploads++;
    this.stats.uploadMs += performance.now() - started;
    pool.meta[slot] = { key, ...meta };
    pool.keyToSlot.set(key, slot);
    pool.usedInFrame[slot] = background ? 0 : this.#frame;
    return slot;
  }

  /** Set the per-frame uniforms, clearing the canvas first unless `clear` is false (paint over the previous frame). */
  beginPaint(f, { clear }) {
    const gl = this.#gl;
    gl.viewport(0, 0, f.width, f.height);
    if (clear) {
      gl.clearColor(...BACKGROUND, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
    gl.uniform2f(this.#uniforms.u_canvas, f.width, f.height);
    gl.uniform3f(this.#uniforms.u_view, f.cx, f.cy, f.scale);
    gl.uniform1i(this.#uniforms.u_product, f.shader);
    gl.uniform3i(this.#uniforms.u_inputs, ...f.inputs);
    gl.uniform1f(this.#uniforms.u_stretch_lo, f.stretchLo);
    gl.uniform1i(this.#uniforms.u_nodata, f.nodata);
  }

  /**
   * Draw one cell. `world` = {x, y, w, h} in level-0 pixels; `extent` = valid texels {w, h}.
   * `deltaSlot` is -1 when the timestep is an anchor.
   */
  drawCell(world, extent, anchorSlot, deltaSlot) {
    const gl = this.#gl;
    const nBand = this.#pool.nBand;
    gl.uniform4f(this.#uniforms.u_cell, world.x, world.y, world.w, world.h);
    gl.uniform2f(this.#uniforms.u_extent, extent.w, extent.h);
    gl.uniform1i(this.#uniforms.u_anchorBase, anchorSlot * nBand);
    gl.uniform1i(this.#uniforms.u_deltaBase, deltaSlot < 0 ? -1 : deltaSlot * nBand);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  /** The canvas as RGBA bytes, bottom row first (GL order). */
  readFrame(width, height) {
    const gl = this.#gl;
    const pixels = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return pixels;
  }

  /** Release the GL context (an offscreen renderer that is finished with). */
  dispose() {
    this.#gl.getExtension('WEBGL_lose_context')?.loseContext();
  }

  /** Block until queued GL work has finished (reads one pixel). For timing only. */
  finish() {
    const gl = this.#gl;
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
  }
}
