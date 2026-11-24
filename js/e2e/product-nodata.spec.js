import { expect, test } from './fixtures.js';

test('viewer and MapLibre shaders keep missing derived inputs out of product colors', async ({ page, servers }) => {
  await page.route('**/product-nodata-probe.html', (route) => route.fulfill({ contentType: 'text/html', body: '<canvas width="1" height="1"></canvas>' }));
  await page.goto(`${servers.appUrl}/product-nodata-probe.html`);
  const result = await page.evaluate(async () => {
    const { fragmentShader: viewerShader } = await import('/demo/renderer.js');
    const { fragmentShader: mapShader } = await import('/maplibre/shader.js');
    const gl = document.querySelector('canvas').getContext('webgl2', { antialias: false, preserveDrawingBuffer: true });
    if (!gl) throw new Error('WebGL2 unavailable');
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const vertex = `#version 300 es
      out vec2 v_texel;
      void main() {
        vec2 position = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));
        gl_Position = vec4(position * 2.0 - 1.0, 0.0, 1.0);
        v_texel = vec2(0.5);
      }`;
    const compile = (type, source) => {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
      return shader;
    };
    const texture = (unit, internal, format, type, data) => {
      const handle = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, handle);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texStorage3D(gl.TEXTURE_2D_ARRAY, 1, internal, 1, 1, data.length);
      gl.texSubImage3D(gl.TEXTURE_2D_ARRAY, 0, 0, 0, 0, 1, 1, data.length, format, type, data);
      return handle;
    };
    const cases = [];
    for (const backend of ['viewer', 'maplibre']) {
      const background = backend === 'viewer' ? [9, 12, 18, 255] : [0, 0, 0, 0];
      for (const dtype of ['uint16', 'float32']) for (const hasMask of [false, true]) {
        const program = gl.createProgram();
        const shaders = [compile(gl.VERTEX_SHADER, vertex), compile(gl.FRAGMENT_SHADER,
          backend === 'viewer' ? viewerShader(dtype, hasMask) : mapShader({ dtype, hasMask }))];
        for (const shader of shaders) gl.attachShader(program, shader);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
        gl.useProgram(program);
        const uniform = (name) => gl.getUniformLocation(program, name);
        gl.uniform1i(uniform('u_data'), 0);
        gl.uniform1i(uniform('u_mask'), 1);
        gl.uniform1i(uniform('u_dataBase'), 0);
        gl.uniform1i(uniform('u_maskLayer'), 0);
        gl.uniform2f(uniform('u_extent'), 1, 1);
        gl.uniform3f(uniform('u_scale'), 1, 1, 1);
        gl.uniform3f(uniform('u_divisor'), 0, 0, 0);
        gl.uniform3f(uniform('u_offset'), 0, 0, 0);
        gl.uniform3f(uniform('u_rangeLo'), -30, -30, -30);
        gl.uniform3f(uniform('u_rangeHi'), 30, 30, 30);
        gl.uniform1f(uniform('u_stretch_lo'), 0);
        gl.uniform1f(uniform('u_stretchLo'), 0);
        gl.uniform1f(uniform('u_opacity'), 1);
        function draw(label, product, values, { nodata = 0, mask = 1, inputs = [0, 1, -1], missing = false } = {}) {
          const float = dtype === 'float32';
          const data = texture(0, float ? gl.R32F : gl.R16UI, float ? gl.RED : gl.RED_INTEGER,
            float ? gl.FLOAT : gl.UNSIGNED_SHORT, float ? new Float32Array(values) : new Uint16Array(values));
          const validity = hasMask ? texture(1, gl.R8UI, gl.RED_INTEGER, gl.UNSIGNED_BYTE, new Uint8Array([mask])) : null;
          gl.uniform3iv(uniform('u_inputs'), inputs);
          gl.uniform1i(uniform('u_product'), product);
          gl.uniform1i(uniform('u_display'), product === 6 || product === 5 ? 1 : 0);
          gl.uniform1i(uniform('u_hasNodata'), !hasMask && nodata !== null ? 1 : 0);
          gl.uniform1f(uniform('u_nodata'), nodata ?? 0);
          gl.clearColor(0, 0, 0, 0);
          gl.clear(gl.COLOR_BUFFER_BIT);
          gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
          const rgba = new Uint8Array(4);
          gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
          cases.push({ label: `${backend}/${dtype}/${hasMask ? 'mask' : 'plain'}/${label}`, rgba: Array.from(rgba), background, missing });
          gl.deleteTexture(data);
          if (validity) gl.deleteTexture(validity);
        }
        for (const product of [2, 3, 4, 6]) {
          if (!hasMask) for (const slot of [0, 1]) {
            const values = [1000, 2000]; values[slot] = 0;
            draw(`${product}/nodata-${slot}`, product, values, { missing: true });
          }
          if (dtype === 'float32') for (const slot of [0, 1]) {
            const values = [1000, 2000]; values[slot] = NaN;
            draw(`${product}/nan-${slot}`, product, values, { missing: true });
          }
          if (hasMask) draw(`${product}/masked`, product, [1000, 2000], { mask: 0, missing: true });
          draw(`${product}/valid-zero`, product, [0, 1000], { nodata: hasMask ? 0 : null });
        }
        draw('single-band-unused-slots', 5, [10, 20], { inputs: [0, -1, -1] });
        gl.deleteProgram(program);
        for (const shader of shaders) gl.deleteShader(shader);
      }
    }
    return { cases, error: gl.getError() };
  });
  expect(result.error).toBe(0);
  expect(result.cases).toHaveLength(120);
  for (const { label, rgba, background, missing } of result.cases) {
    if (missing) expect(rgba, label).toEqual(background);
    else {
      expect(rgba, label).not.toEqual(background);
      expect(rgba[3], label).toBe(255);
    }
  }
});
