// WebGL2 renderer for chronozarr cells.
//
// All chunks live as raw values of the store's data type in one TEXTURE_2D_ARRAY (R8UI, R16UI, R16I or R32F,
// see TEXTURE_FORMATS). A chunk (band, y, x) occupies n_band consecutive layers ("slot"), so uploading one is a
// single texSubImage3D from the decoded array. The fragment shader reads the anchor slot and, for delta
// timesteps, the delta slot and adds them per pixel, so switching timesteps or products never runs a CPU loop
// over pixels. The product colors come from products-glsl.js.

import { PRODUCT_GLSL } from './products-glsl.js';

const BACKGROUND = [0.035, 0.047, 0.071];

/**
 * How each stored data type lives on the GPU: texture format, upload types, and the GLSL that reads a value as a
 * float. The 8- and 16-bit unsigned types add the delta to the anchor modulo 2^bits (chronozarr v0.2 residuals).
 */
export const TEXTURE_FORMATS = {
  uint8: { internal: 'R8UI', format: 'RED_INTEGER', type: 'UNSIGNED_BYTE', Array: Uint8Array, sampler: 'usampler2DArray', delta: 255 },
  uint16: { internal: 'R16UI', format: 'RED_INTEGER', type: 'UNSIGNED_SHORT', Array: Uint16Array, sampler: 'usampler2DArray', delta: 65535 },
  int16: { internal: 'R16I', format: 'RED_INTEGER', type: 'SHORT', Array: Int16Array, sampler: 'isampler2DArray', delta: null },
  float32: { internal: 'R32F', format: 'RED', type: 'FLOAT', Array: Float32Array, sampler: 'sampler2DArray', delta: null },
};

function valueGlsl({ sampler, delta }, dtype) {
  if (delta === null) {
    return `
float value(ivec2 texel, int band) {
  if (band < 0) return 0.0;
  return float(texelFetch(u_data, ivec3(texel, u_anchorBase + band), 0).r);
}`;
  }
  return `
// star-delta: value = (anchor + residual) mod 2^bits
float value(ivec2 texel, int band) {
  if (band < 0) return 0.0;
  uint a = texelFetch(u_data, ivec3(texel, u_anchorBase + band), 0).r;
  if (u_deltaBase < 0) return float(a);
  uint d = texelFetch(u_data, ivec3(texel, u_deltaBase + band), 0).r;
  return float((a + d) & ${delta}u);
}`;
}

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

export function fragmentShader(dtype) {
  const format = TEXTURE_FORMATS[dtype];
  const isFloat = dtype === 'float32';
  return `#version 300 es
precision highp float;
precision highp int;
precision highp ${format.sampler};
in vec2 v_texel;
uniform ${format.sampler} u_data;
uniform vec2 u_extent;
uniform int u_anchorBase;   // first layer of the anchor slot
uniform int u_deltaBase;    // first layer of the delta slot; -1 when the timestep is itself an anchor
uniform ivec3 u_inputs;     // band index per product input, -1 = unused
uniform int u_product;
uniform int u_display;      // 0 = tone-mapped reflectance, 1 = linear stretch of u_range
uniform vec2 u_range;
uniform float u_stretch_lo;
uniform int u_hasNodata;
uniform float u_nodata;
uniform vec3 u_scale;       // per input: physical = stored * scale + offset ...
uniform vec3 u_divisor;     // ... or stored / divisor + offset where the divisor is above zero
uniform vec3 u_offset;
out vec4 outColor;

const vec3 BG = vec3(${BACKGROUND.join(', ')});
${valueGlsl(format, dtype)}

bool isNodata(float v) {
  return (u_hasNodata != 0 && v == u_nodata)${isFloat ? ' || isnan(v)' : ''};
}

vec3 toPhysical(vec3 v) {
  vec3 scaled = vec3(
    u_divisor.x > 0.0 ? v.x / u_divisor.x : v.x * u_scale.x,
    u_divisor.y > 0.0 ? v.y / u_divisor.y : v.y * u_scale.y,
    u_divisor.z > 0.0 ? v.z / u_divisor.z : v.z * u_scale.z);
  return scaled + u_offset;
}
${PRODUCT_GLSL}
void main() {
  ivec2 texel = ivec2(min(floor(v_texel), u_extent - 1.0));
  float v0 = value(texel, u_inputs.x);
  float v1 = value(texel, u_inputs.y);
  float v2 = value(texel, u_inputs.z);
  bool empty = (u_inputs.x < 0 || isNodata(v0)) && (u_inputs.y < 0 || isNodata(v1)) && (u_inputs.z < 0 || isNodata(v2));
  if (empty) {
    outColor = vec4(BG, 1.0);
    return;
  }
  vec3 x = toPhysical(vec3(v0, v1, v2));
  outColor = u_display == 1 ? shadeLinear(u_product, x, u_range) : shade(u_product, x, u_stretch_lo);
}`;
}

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

const UNIFORM_NAMES = ['u_canvas', 'u_view', 'u_cell', 'u_extent', 'u_data', 'u_anchorBase', 'u_deltaBase', 'u_inputs', 'u_product', 'u_display', 'u_range', 'u_stretch_lo', 'u_hasNodata', 'u_nodata', 'u_scale', 'u_divisor', 'u_offset'];

export class Renderer {
  #gl;
  #programs = new Map();
  #uniforms = {};
  #texture = null;
  #pool = null;
  #frame = 0;
  #dtype = 'uint16';

  /** Called with a slot's cell metadata; the highest score is evicted first. Set by the viewer. */
  evictionScore = () => 0;
  stats = { uploads: 0, uploadMs: 0, evictions: 0 };

  constructor(canvas) {
    const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, preserveDrawingBuffer: true });
    if (!gl) throw new Error('WebGL2 is not available in this browser.');
    this.#gl = gl;
    this.#useProgram('uint16');
    this.limits = { maxLayers: gl.getParameter(gl.MAX_ARRAY_TEXTURE_LAYERS), maxSize: gl.getParameter(gl.MAX_TEXTURE_SIZE) };
  }

  /** Select (compiling on first use) the shader program that reads textures of this data type. */
  #useProgram(dtype) {
    const gl = this.#gl;
    if (!TEXTURE_FORMATS[dtype]) throw new Error(`Unsupported data type ${dtype}; the viewer shows ${Object.keys(TEXTURE_FORMATS).join(', ')}.`);
    let entry = this.#programs.get(dtype);
    if (!entry) {
      const program = gl.createProgram();
      const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
      const fs = compile(gl, gl.FRAGMENT_SHADER, fragmentShader(dtype));
      gl.attachShader(program, vs);
      gl.attachShader(program, fs);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(`Program link failed: ${gl.getProgramInfoLog(program)}`);
      }
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      gl.useProgram(program);
      const uniforms = Object.fromEntries(UNIFORM_NAMES.map((name) => [name, gl.getUniformLocation(program, name)]));
      gl.uniform1i(uniforms.u_data, 0);
      entry = { program, uniforms };
      this.#programs.set(dtype, entry);
    }
    gl.useProgram(entry.program);
    this.#uniforms = entry.uniforms;
    this.#dtype = dtype;
  }

  get dtype() {
    return this.#dtype;
  }

  get slots() {
    return this.#pool?.slots ?? 0;
  }

  /** Number of slots that fit `budgetBytes` and the driver's layer limit (`bytesPerSample`: 1, 2 or 4 by data type). */
  planSlots(nBand, chunkWidth, chunkHeight, budgetBytes, wanted, bytesPerSample = 2) {
    const byLayers = Math.floor(this.limits.maxLayers / nBand);
    const byBytes = Math.floor(budgetBytes / (nBand * chunkWidth * chunkHeight * bytesPerSample));
    return Math.max(0, Math.min(wanted, byLayers, byBytes));
  }

  /** (Re)allocate the texture pool for a data type (default uint16). Drops every resident chunk. */
  configure({ dtype = 'uint16', nBand, chunkWidth, chunkHeight, slots }) {
    const gl = this.#gl;
    this.#useProgram(dtype);
    const format = TEXTURE_FORMATS[dtype];
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, format.Array.BYTES_PER_ELEMENT);
    if (chunkWidth > this.limits.maxSize || chunkHeight > this.limits.maxSize) {
      throw new Error(`Chunk ${chunkWidth}x${chunkHeight} exceeds MAX_TEXTURE_SIZE ${this.limits.maxSize}.`);
    }
    if (slots < 2) throw new Error(`Texture pool has ${slots} slots; at least 2 are needed (anchor + delta).`);
    if (this.#texture) gl.deleteTexture(this.#texture);
    this.#texture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.#texture);
    gl.texStorage3D(gl.TEXTURE_2D_ARRAY, 1, gl[format.internal], chunkWidth, chunkHeight, slots * nBand);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.#pool = {
      dtype,
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
    const format = TEXTURE_FORMATS[pool.dtype];
    // The reader may hand over the bits of a signed type as unsigned (or the reverse); a view keeps the same buffer.
    const typed = data instanceof format.Array ? data : new format.Array(data.buffer, data.byteOffset, data.byteLength / format.Array.BYTES_PER_ELEMENT);
    const started = performance.now();
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.#texture);
    gl.texSubImage3D(gl.TEXTURE_2D_ARRAY, 0, 0, 0, slot * pool.nBand, pool.chunkWidth, pool.chunkHeight, pool.nBand, gl[format.format], gl[format.type], typed);
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
    gl.uniform1i(this.#uniforms.u_display, f.display === 'linear' ? 1 : 0);
    gl.uniform2f(this.#uniforms.u_range, ...(f.range ?? [0, 1]));
    gl.uniform1i(this.#uniforms.u_hasNodata, f.nodata === null ? 0 : 1);
    gl.uniform1f(this.#uniforms.u_nodata, f.nodata ?? 0);
    gl.uniform3f(this.#uniforms.u_scale, ...f.unitScale);
    gl.uniform3f(this.#uniforms.u_divisor, ...f.unitDivisor);
    gl.uniform3f(this.#uniforms.u_offset, ...f.unitOffset);
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
