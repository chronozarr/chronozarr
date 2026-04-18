/**
 * spacetime WebGL2 shader renderer
 * Band unpacking and product visualization for Sentinel-2 L2A data
 */

// Vertex shader (shared by all products)
const VERTEX_SHADER = `#version 300 es
in vec2 a_position;
in vec2 a_texCoord;
out vec2 v_texCoord;
void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texCoord = a_texCoord;
}
`;

// True color fragment shader - Sentinel Hub L2A optimized pipeline
const TRUE_COLOR_FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture0;  // R=B02_hi, G=B02_lo, B=B03_hi, A=B03_lo
uniform sampler2D u_texture1;  // R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
uniform vec2 u_resolution;
out vec4 outColor;

// Constants from render.py
const float TC_MAX_R = 1.5;
const float TC_MID_R = 0.13;
const float TC_GAMMA = 1.5;
const float TC_SAT = 1.5;
const float TC_G_OFF = 0.01;

// Reconstruct uint16 from hi/lo bytes
float decodeUint16(float hi, float lo) {
    return hi * 256.0 + lo;
}

// Convert uint16 reflectance to float [0, 1]
float toReflectance(float val) {
    return val / 10000.0;
}

// Highlight compression - rational curve
float highlightCompress(float a) {
    float ar = clamp(a / TC_MAX_R, 0.0, 1.0);
    float tx_norm = TC_MID_R / TC_MAX_R;
    float num = ar * (ar * tx_norm - 1.0);
    float den = ar * (2.0 * tx_norm - 1.0) - tx_norm;
    if (abs(den) > 1e-10) {
        return clamp(num / den, 0.0, 1.0);
    }
    return 0.0;
}

// Gamma with offset
float adjGamma(float b) {
    float g_off_pow = pow(TC_G_OFF, TC_GAMMA);
    float g_off_range = pow(1.0 + TC_G_OFF, TC_GAMMA) - g_off_pow;
    return (pow(b + TC_G_OFF, TC_GAMMA) - g_off_pow) / g_off_range;
}

// Combined highlight compression + gamma
float sAdj(float a) {
    return adjGamma(highlightCompress(a));
}

// Saturation enhancement
vec3 satEnhance(vec3 rgb, float sat) {
    float avg = (rgb.r + rgb.g + rgb.b) / 3.0 * (1.0 - sat);
    return clamp(vec3(avg + rgb.r * sat, avg + rgb.g * sat, avg + rgb.b * sat), 0.0, 1.0);
}

// Linear to sRGB transfer function
float linearToSrgb(float linear) {
    if (linear <= 0.0031308) {
        return 12.92 * linear;
    }
    return 1.055 * pow(linear, 1.0 / 2.4) - 0.055;
}

vec3 linearToSrgb(vec3 linear) {
    return vec3(linearToSrgb(linear.r), linearToSrgb(linear.g), linearToSrgb(linear.b));
}

void main() {
    vec4 tex0 = texture(u_texture0, v_texCoord);
    vec4 tex1 = texture(u_texture1, v_texCoord);
    
    // Reconstruct bands from packed textures
    float b02 = decodeUint16(tex0.r, tex0.g);  // Blue
    float b03 = decodeUint16(tex0.b, tex0.a);  // Green
    float b04 = decodeUint16(tex1.r, tex1.g);  // Red
    // float b08 = decodeUint16(tex1.b, tex1.a);  // NIR (not used in true color)
    
    // Check for nodata (all zeros)
    if (b02 == 0.0 && b03 == 0.0 && b04 == 0.0) {
        outColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    
    // Convert to reflectance [0, 1]
    float r = toReflectance(b04);
    float g = toReflectance(b03);
    float b = toReflectance(b02);
    
    // Highlight compression + gamma
    vec3 adj = vec3(sAdj(r), sAdj(g), sAdj(b));
    
    // Saturation boost
    vec3 sat = satEnhance(adj, TC_SAT);
    
    // sRGB encoding
    vec3 srgb = linearToSrgb(sat);
    
    outColor = vec4(clamp(srgb, 0.0, 1.0), 1.0);
}
`;

// False color fragment shader - NIR/Red/Green with fixed stretch (0, 10000) -> (0, 1)
const FALSE_COLOR_FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture0;  // R=B02_hi, G=B02_lo, B=B03_hi, A=B03_lo
uniform sampler2D u_texture1;  // R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
out vec4 outColor;

float decodeUint16(float hi, float lo) {
    return hi * 256.0 + lo;
}

float linearToSrgb(float linear) {
    if (linear <= 0.0031308) {
        return 12.92 * linear;
    }
    return 1.055 * pow(linear, 1.0 / 2.4) - 0.055;
}

void main() {
    vec4 tex0 = texture(u_texture0, v_texCoord);
    vec4 tex1 = texture(u_texture1, v_texCoord);
    
    // Reconstruct bands: NIR (B08), Red (B04), Green (B03)
    float nir = decodeUint16(tex1.b, tex1.a);
    float red = decodeUint16(tex1.r, tex1.g);
    float green = decodeUint16(tex0.b, tex0.a);
    
    // Check for nodata
    if (nir == 0.0 && red == 0.0 && green == 0.0) {
        outColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    
    // Fixed stretch: (0, 10000) -> (0, 1)
    const float lo = 0.0;
    const float hi = 10000.0;
    
    vec3 rgb;
    rgb.r = clamp((nir - lo) / (hi - lo), 0.0, 1.0);  // NIR -> Red channel
    rgb.g = clamp((red - lo) / (hi - lo), 0.0, 1.0);   // Red -> Green channel
    rgb.b = clamp((green - lo) / (hi - lo), 0.0, 1.0); // Green -> Blue channel
    
    // sRGB gamma
    rgb = vec3(linearToSrgb(rgb.r), linearToSrgb(rgb.g), linearToSrgb(rgb.b));
    
    outColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
`;

// NDVI fragment shader - diverging colormap with fixed threshold at 0.2
const NDVI_FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture1;  // R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
out vec4 outColor;

float decodeUint16(float hi, float lo) {
    return hi * 256.0 + lo;
}

// Color stops from render.py (normalized to 0-1)
const vec3 c_water = vec3(26.0, 35.0, 126.0) / 255.0;    // dark indigo
const vec3 c_shadow = vec3(69.0, 90.0, 100.0) / 255.0;   // blue-gray
const vec3 c_brown = vec3(93.0, 64.0, 55.0) / 255.0;     // dark brown
const vec3 c_tan = vec3(215.0, 204.0, 200.0) / 255.0;    // warm tan
const vec3 c_lgreen = vec3(174.0, 213.0, 129.0) / 255.0; // light green
const vec3 c_dgreen = vec3(27.0, 94.0, 32.0) / 255.0;    // deep forest green

// Fixed threshold (skip Otsu for now)
const float thresh = 0.2;

void main() {
    vec4 tex1 = texture(u_texture1, v_texCoord);
    
    // Reconstruct NIR and Red bands
    float nir = decodeUint16(tex1.b, tex1.a);
    float red = decodeUint16(tex1.r, tex1.g);
    
    // Check for nodata
    float denom = nir + red;
    if (denom <= 0.0) {
        outColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    
    // Compute NDVI: (NIR - Red) / (NIR + Red)
    float ndvi = (nir - red) / denom;
    
    vec3 color;
    
    // Segment 1: water/shadow - NDVI < 0
    if (ndvi < 0.0) {
        float t = clamp(ndvi + 1.0, 0.0, 1.0);  // [-1, 0] -> [0, 1]
        color = c_water + (c_shadow - c_water) * t;
    }
    // Segment 2: bare soil - 0 <= NDVI < threshold
    else if (ndvi < thresh) {
        float t = clamp(ndvi / thresh, 0.0, 1.0);
        color = c_brown + (c_tan - c_brown) * t;
    }
    // Segment 3: vegetation - NDVI >= threshold
    else {
        float t = clamp((ndvi - thresh) / (1.0 - thresh), 0.0, 1.0);
        color = c_lgreen + (c_dgreen - c_lgreen) * t;
    }
    
    outColor = vec4(color, 1.0);
}
`;

// NDWI fragment shader - diverging colormap (brown -> gray -> blue)
const NDWI_FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture0;  // R=B02_hi, G=B02_lo, B=B03_hi, A=B03_lo
uniform sampler2D u_texture1;  // R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
out vec4 outColor;

float decodeUint16(float hi, float lo) {
    return hi * 256.0 + lo;
}

// Color stops from render.py
const vec3 c_brown = vec3(181.0, 101.0, 29.0) / 255.0;   // dry
const vec3 c_gray = vec3(192.0, 192.0, 192.0) / 255.0;    // neutral
const vec3 c_blue = vec3(26.0, 82.0, 118.0) / 255.0;      // wet

void main() {
    vec4 tex0 = texture(u_texture0, v_texCoord);
    vec4 tex1 = texture(u_texture1, v_texCoord);
    
    // Reconstruct Green and NIR bands
    float green = decodeUint16(tex0.b, tex0.a);
    float nir = decodeUint16(tex1.b, tex1.a);
    
    // Check for nodata
    float denom = green + nir;
    if (denom <= 0.0) {
        outColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    
    // Compute NDWI: (Green - NIR) / (Green + NIR)
    float ndwi = (green - nir) / denom;
    
    // Map to colormap: [-1, 1] -> [0, 1]
    float t = clamp((ndwi + 1.0) / 2.0, 0.0, 1.0);
    
    vec3 color;
    // Brown (0) -> gray (0.5) -> blue (1)
    if (t < 0.5) {
        float s = t / 0.5;
        color = c_brown + (c_gray - c_brown) * s;
    } else {
        float s = (t - 0.5) / 0.5;
        color = c_gray + (c_blue - c_gray) * s;
    }
    
    outColor = vec4(color, 1.0);
}
`;

// Water fragment shader - NDWI > 0 threshold
const WATER_FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_texCoord;
uniform sampler2D u_texture0;  // R=B02_hi, G=B02_lo, B=B03_hi, A=B03_lo
uniform sampler2D u_texture1;  // R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
out vec4 outColor;

float decodeUint16(float hi, float lo) {
    return hi * 256.0 + lo;
}

// Colors from render.py
const vec3 c_water = vec3(41.0, 128.0, 185.0) / 255.0;    // blue (#2980b9)
const vec3 c_dark = vec3(26.0, 26.0, 46.0) / 255.0;       // dark gray

void main() {
    vec4 tex0 = texture(u_texture0, v_texCoord);
    vec4 tex1 = texture(u_texture1, v_texCoord);
    
    // Reconstruct Green and NIR bands
    float green = decodeUint16(tex0.b, tex0.a);
    float nir = decodeUint16(tex1.b, tex1.a);
    
    // Check for nodata
    float denom = green + nir;
    if (denom <= 0.0) {
        outColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    
    // Compute NDWI and classify
    float ndwi = (green - nir) / denom;
    bool isWater = ndwi > 0.0;
    
    vec3 color = isWater ? c_water : c_dark;
    
    outColor = vec4(color, 1.0);
}
`;

// Product shader registry
const PRODUCT_SHADERS = {
    true_color: TRUE_COLOR_FRAGMENT,
    false_color: FALSE_COLOR_FRAGMENT,
    ndvi: NDVI_FRAGMENT,
    ndwi: NDWI_FRAGMENT,
    water: WATER_FRAGMENT,
};

/**
 * ShaderRenderer class - WebGL2-based product visualization
 */
class ShaderRenderer {
    constructor(canvas) {
        this.canvas = canvas;
        this.gl = null;
        this.program = null;
        this.positionBuffer = null;
        this.texCoordBuffer = null;
        this.texture0 = null;
        this.texture1 = null;
        this.currentProduct = null;
        this.width = 0;
        this.height = 0;
        this.programs = new Map();
        
        this._initWebGL();
        this._setupGeometry();
        
        // Handle context loss
        canvas.addEventListener('webglcontextlost', (e) => {
            e.preventDefault();
            this._cleanup();
        });
        canvas.addEventListener('webglcontextrestored', () => {
            this._initWebGL();
            this._setupGeometry();
            if (this.currentProduct) {
                this.switchProduct(this.currentProduct);
            }
        });
    }
    
    _initWebGL() {
        this.gl = this.canvas.getContext('webgl2', {
            alpha: false,
            premultipliedAlpha: false,
            preserveDrawingBuffer: false,
            antialias: false,
        });
        
        if (!this.gl) {
            throw new Error('WebGL2 not supported');
        }
    }
    
    _cleanup() {
        const gl = this.gl;
        if (!gl) return;
        
        // Delete textures
        if (this.texture0) {
            gl.deleteTexture(this.texture0);
            this.texture0 = null;
        }
        if (this.texture1) {
            gl.deleteTexture(this.texture1);
            this.texture1 = null;
        }
        
        // Delete programs
        this.programs.forEach((prog) => {
            gl.deleteProgram(prog.program);
            gl.deleteShader(prog.vs);
            gl.deleteShader(prog.fs);
        });
        this.programs.clear();
        this.program = null;
        
        // Delete buffers
        if (this.positionBuffer) {
            gl.deleteBuffer(this.positionBuffer);
            this.positionBuffer = null;
        }
        if (this.texCoordBuffer) {
            gl.deleteBuffer(this.texCoordBuffer);
            this.texCoordBuffer = null;
        }
    }
    
    _compileShader(type, source) {
        const gl = this.gl;
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            const info = gl.getShaderInfoLog(shader);
            gl.deleteShader(shader);
            throw new Error('Shader compilation error: ' + info);
        }
        
        return shader;
    }
    
    _createProgram(vsSource, fsSource) {
        const gl = this.gl;
        const vs = this._compileShader(gl.VERTEX_SHADER, vsSource);
        const fs = this._compileShader(gl.FRAGMENT_SHADER, fsSource);
        
        const program = gl.createProgram();
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);
        
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            const info = gl.getProgramInfoLog(program);
            gl.deleteProgram(program);
            gl.deleteShader(vs);
            gl.deleteShader(fs);
            throw new Error('Program linking error: ' + info);
        }
        
        return { program, vs, fs };
    }
    
    _getOrCreateProgram(product) {
        if (this.programs.has(product)) {
            return this.programs.get(product);
        }
        
        const fsSource = PRODUCT_SHADERS[product];
        if (!fsSource) {
            throw new Error('Unknown product: ' + product);
        }
        
        const progInfo = this._createProgram(VERTEX_SHADER, fsSource);
        this.programs.set(product, progInfo);
        return progInfo;
    }
    
    _setupGeometry() {
        const gl = this.gl;
        
        // Fullscreen quad positions (clip space: -1 to 1)
        const positions = new Float32Array([
            -1, -1,
             1, -1,
            -1,  1,
            -1,  1,
             1, -1,
             1,  1,
        ]);
        
        // Texture coordinates (0 to 1, flipped Y for WebGL)
        const texCoords = new Float32Array([
            0, 1,
            1, 1,
            0, 0,
            0, 0,
            1, 1,
            1, 0,
        ]);
        
        this.positionBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.positionBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
        
        this.texCoordBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.texCoordBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, texCoords, gl.STATIC_DRAW);
    }
    
    _createTexture(data, width, height) {
        const gl = this.gl;
        const texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        return texture;
    }
    
    /**
     * Load band data from raw endpoint ArrayBuffer
     * @param {ArrayBuffer} buffer - Raw binary data with 8-byte header
     */
    loadBandData(buffer) {
        // Parse 8-byte header: n_bands (2), height (2), width (2), reserved (2)
        const header = new Uint16Array(buffer.slice(0, 8));
        const bands = header[0];
        const height = header[1];
        const width = header[2];
        // header[3] is reserved

        if (bands < 4) {
            throw new Error('Expected at least 4 bands, got: ' + bands);
        }
        
        // Raw uint16 band data
        const uint16Data = new Uint16Array(buffer, 8, width * height * bands);
        
        // Create packed RGBA8 textures
        // Packing: B02 (hi/lo) + B03 (hi/lo) in texture0
        //          B04 (hi/lo) + B08 (hi/lo) in texture1
        const tex0Data = new Uint8Array(width * height * 4);
        const tex1Data = new Uint8Array(width * height * 4);
        
        const pixels = width * height;
        for (let i = 0; i < pixels; i++) {
            const b02 = uint16Data[i * bands + 0];  // Blue
            const b03 = uint16Data[i * bands + 1];  // Green
            const b04 = uint16Data[i * bands + 2];  // Red
            const b08 = uint16Data[i * bands + 3];  // NIR
            
            // Texture 0: R=B02_hi, G=B02_lo, B=B03_hi, A=B03_lo
            tex0Data[i * 4 + 0] = (b02 >> 8) & 0xFF;  // hi
            tex0Data[i * 4 + 1] = b02 & 0xFF;         // lo
            tex0Data[i * 4 + 2] = (b03 >> 8) & 0xFF;  // hi
            tex0Data[i * 4 + 3] = b03 & 0xFF;         // lo
            
            // Texture 1: R=B04_hi, G=B04_lo, B=B08_hi, A=B08_lo
            tex1Data[i * 4 + 0] = (b04 >> 8) & 0xFF;  // hi
            tex1Data[i * 4 + 1] = b04 & 0xFF;         // lo
            tex1Data[i * 4 + 2] = (b08 >> 8) & 0xFF;  // hi
            tex1Data[i * 4 + 3] = b08 & 0xFF;         // lo
        }
        
        // Update textures
        const gl = this.gl;
        
        if (this.texture0) {
            gl.deleteTexture(this.texture0);
        }
        if (this.texture1) {
            gl.deleteTexture(this.texture1);
        }
        
        this.texture0 = this._createTexture(tex0Data, width, height);
        this.texture1 = this._createTexture(tex1Data, width, height);
        this.width = width;
        this.height = height;
        
        // Resize canvas to match
        this.canvas.width = width;
        this.canvas.height = height;
        
        // Re-render if we have a product selected
        if (this.currentProduct) {
            this.render();
        }
        
        return { width, height, bands };
    }
    
    /**
     * Switch to a different product visualization
     * @param {string} product - One of: true_color, false_color, ndvi, ndwi, water
     */
    switchProduct(product) {
        if (!PRODUCT_SHADERS[product]) {
            throw new Error('Unknown product: ' + product);
        }
        
        this.currentProduct = product;
        const progInfo = this._getOrCreateProgram(product);
        this.program = progInfo.program;
        
        // Bind attributes
        const gl = this.gl;
        const positionLoc = gl.getAttribLocation(this.program, 'a_position');
        const texCoordLoc = gl.getAttribLocation(this.program, 'a_texCoord');
        
        gl.useProgram(this.program);
        
        // Setup position attribute
        gl.bindBuffer(gl.ARRAY_BUFFER, this.positionBuffer);
        gl.enableVertexAttribArray(positionLoc);
        gl.vertexAttribPointer(positionLoc, 2, gl.FLOAT, false, 0, 0);
        
        // Setup texCoord attribute
        gl.bindBuffer(gl.ARRAY_BUFFER, this.texCoordBuffer);
        gl.enableVertexAttribArray(texCoordLoc);
        gl.vertexAttribPointer(texCoordLoc, 2, gl.FLOAT, false, 0, 0);
        
        this.render();
    }
    
    /**
     * Render the current frame
     */
    render() {
        if (!this.program || !this.texture0 || !this.texture1) {
            return;
        }
        
        const gl = this.gl;
        
        gl.viewport(0, 0, this.canvas.width, this.canvas.height);
        gl.clearColor(0.0, 0.0, 0.0, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        
        gl.useProgram(this.program);
        
        // Bind textures
        const loc0 = gl.getUniformLocation(this.program, 'u_texture0');
        const loc1 = gl.getUniformLocation(this.program, 'u_texture1');
        const resLoc = gl.getUniformLocation(this.program, 'u_resolution');
        
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.texture0);
        gl.uniform1i(loc0, 0);
        
        // Only bind texture1 if the shader uses it
        if (loc1 !== null) {
            gl.activeTexture(gl.TEXTURE1);
            gl.bindTexture(gl.TEXTURE_2D, this.texture1);
            gl.uniform1i(loc1, 1);
        }
        
        if (resLoc !== null) {
            gl.uniform2f(resLoc, this.width, this.height);
        }
        
        gl.drawArrays(gl.TRIANGLES, 0, 6);
    }
    
    /**
     * Fetch and load band data from a raw endpoint URL
     * @param {string} url - Raw data endpoint URL
     * @returns {Promise<{width: number, height: number, bands: number}>}
     */
    async fetchBandData(url) {
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error('Failed to fetch: ' + response.status + ' ' + response.statusText);
        }
        const buffer = await response.arrayBuffer();
        return this.loadBandData(buffer);
    }
    
    /**
     * Clean up all WebGL resources
     */
    destroy() {
        this._cleanup();
        if (this.gl) {
            const ext = this.gl.getExtension('WEBGL_lose_context');
            if (ext) {
                ext.loseContext();
            }
        }
    }
}

// Export for module systems or attach to window
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { ShaderRenderer, PRODUCT_SHADERS };
} else if (typeof window !== 'undefined') {
    window.ShaderRenderer = ShaderRenderer;
    window.PRODUCT_SHADERS = PRODUCT_SHADERS;
}
