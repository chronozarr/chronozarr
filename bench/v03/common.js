// Page-side helpers shared by the three drivers (injected before them by session.mjs).
//
//   __bench.gpuDone(canvas)   resolves when the GPU has finished every draw issued so far to the canvas's WebGL2 context
//
// Every system ends a measured interval the same way: its own "this frame is complete" signal, then a one-pixel
// readback, which cannot return before the GPU is done (js/demo/renderer.js `finish()` does the same for the viewer).
// The interval therefore includes the GPU work of the frame for all three systems and excludes what none of them
// control, the browser compositor's presentation at the next vsync.

(() => {
  const scratch = new Uint8Array(4);
  window.__bench = {
    gpuDone(canvas) {
      const gl = canvas.getContext('webgl2');
      if (!gl) throw new Error('canvas has no WebGL2 context: the benchmark needs the GPU path');
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, scratch);
      return performance.now();
    },
  };
})();
