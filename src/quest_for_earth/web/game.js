/*
  Quest for Earth — Level 1 (3-D cube, Portal, fog of war, mini-map).
  No framework, no build step.  Works in the browser alone.

  Controls:
    Mouse    — look (pointer lock or drag; arrows also look)
    W/S      — thrust along the view vector
    A/D, Space/Shift — strafe / rise-sink
    E        — enter the wormhole
    Q        — evade (burst away from the nearest threat)
    C        — scan (reveal nearby debris and mobs)
    F        — harvest nearby debris
    V        — toggle third/first person
    M        — trim HUD
    F2       — fullscreen
*/

// ── three.js would be ideal but we keep it dependency-free.
// We'll use a minimal WebGL setup + basic math.

// ── app state ──────────────────────────────────────────────────────────
let canvas, gl;
let program;
let projMatrix, viewMatrix, modelMatrix;
let normalLoc = -1;    // a_normal attribute (lit meshes)
let emissiveLoc = null; // u_emissive uniform (glowing meshes bypass lighting)

// The ship's rendered heading is visual state derived from the pilot's own
// move intents; the server still owns the authoritative position (D2/D20).
let shipYaw = 0;
let shipPitch = 0;
// Free-look orientation, driven by the mouse (pointer lock or drag) or by the
// arrow keys. Thrust follows this vector, so pitching down and thrusting
// carries the ship toward the point it is looking at.
let viewYaw = 0;
let viewPitch = 0;
const PITCH_LIMIT = 1.35; // ~77°, short of the singular straight-up/down view
const LOOK_SENS = 0.0025;
const DIR_YAW = { north: 0, east: Math.PI / 2, south: Math.PI, west: -Math.PI / 2 };

function viewForward() {
  const cp = Math.cos(viewPitch);
  return [Math.sin(viewYaw) * cp, Math.sin(viewPitch), Math.cos(viewYaw) * cp];
}
// A short rear-thruster puff accompanies each commanded move.
let thrustUntil = 0;
let thrust = 0;
// Muzzle flash: armed while a shot is firing; decays over ~140ms.
let fireUntil = 0;
// Differential engine glow while turning: +1 = turning left (right brighter).
let turnBiasTarget = 0;
let biasUntil = 0;
let engineBias = 0;
// Nose-tip vertical jets: +1 = climbing (underside fires).
let verticalTarget = 0;
let vertUntil = 0;
let verticalThrust = 0;
const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

// ── procedural sound effects (WebAudio synth, no asset files) ────────────
// Created lazily on first use; browsers suspend AudioContext before a user
// gesture, so every entry point goes through ensureAudio().
let actx = null;
let sfxGain = null;
let audioUnavailable = false;
// Returns the AudioContext (or null when audio is not available). Callers use
// it as the context — returning a boolean here silently muted every sound.
function ensureAudio() {
  if (audioUnavailable) return null;
  if (!actx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { audioUnavailable = true; return null; }
    try {
      const ctx = new AC();
      // Some environments expose a partial AudioContext; without the
      // oscillator/gain/buffer API we cannot synthesise anything, so stay
      // silent rather than throwing into the gameplay path.
      if (!ctx || typeof ctx.createOscillator !== "function"
          || typeof ctx.createGain !== "function"
          || typeof ctx.createBuffer !== "function") {
        audioUnavailable = true;
        return null;
      }
      actx = ctx;
    } catch (e) { audioUnavailable = true; return null; }
    try {
      sfxGain = actx.createGain();
      sfxGain.gain.value = 0.35;
      sfxGain.connect(actx.destination);
    } catch (e) { sfxGain = null; }
  }
  if (actx.state === "suspended") { try { actx.resume(); } catch (e) { /* noop */ } }
  return sfxGain ? actx : null;
}
function sfxTone(freq, dur, type, gain, slideTo) {
  try {
    const ac = ensureAudio(); if (!ac || !sfxGain) return;
    const t0 = ac.currentTime;
    const osc = ac.createOscillator();
    const g = ac.createGain();
    osc.type = type || "sine";
    osc.frequency.setValueAtTime(freq, t0);
    if (slideTo) osc.frequency.exponentialRampToValueAtTime(Math.max(20, slideTo), t0 + dur);
    g.gain.setValueAtTime(gain || 0.2, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g); g.connect(sfxGain);
    osc.start(t0); osc.stop(t0 + dur + 0.02);
  } catch (e) { /* audio must never break the game */ }
}
function sfxNoise(dur, gain, freqFrom, freqTo) {
  try {
    const ac = ensureAudio(); if (!ac || !sfxGain) return;
    const t0 = ac.currentTime;
    const len = Math.max(1, Math.floor(ac.sampleRate * dur));
    const buf = ac.createBuffer(1, len, ac.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / len);
    const src = ac.createBufferSource(); src.buffer = buf;
    const f = ac.createBiquadFilter(); f.type = "lowpass";
    f.frequency.setValueAtTime(freqFrom || 2000, t0);
    if (freqTo) f.frequency.exponentialRampToValueAtTime(Math.max(20, freqTo), t0 + dur);
    const g = ac.createGain();
    g.gain.setValueAtTime(gain || 0.2, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    src.connect(f); f.connect(g); g.connect(sfxGain);
    src.start(t0); src.stop(t0 + dur + 0.02);
  } catch (e) { /* audio must never break the game */ }
}
function sfxShoot() { sfxTone(240, 0.09, "square", 0.14, 50); }
function sfxHit() { sfxTone(180, 0.12, "triangle", 0.2, 40); }
function sfxKill() { sfxNoise(0.28, 0.22, 1400, 90); sfxTone(90, 0.28, "sawtooth", 0.16, 30); }
function sfxHarvest() { sfxTone(660, 0.07, "sine", 0.11); setTimeout(() => sfxTone(880, 0.1, "sine", 0.11), 70); }
function sfxEvade() { sfxNoise(0.32, 0.13, 320, 1500); }
function sfxDeath() { sfxTone(320, 0.6, "sawtooth", 0.16, 45); }
function sfxImpact() { sfxNoise(0.18, 0.22, 900, 120); sfxTone(110, 0.16, "square", 0.12, 50); }
function sfxScan() {
  sfxTone(420, 0.32, "sine", 0.12, 1400);
  sfxTone(210, 0.32, "triangle", 0.07, 700);
}
function sfxPortal() {
  sfxTone(90, 0.7, "sine", 0.16, 320);
  sfxNoise(0.7, 0.12, 300, 1800);
}

// Engine bed: a looping noise source whose gain and cutoff follow the
// thrust level, so the ship sounds alive only while it is burning.
let thrustSrc = null;
let thrustGain = null;
let thrustFilter = null;
let thrustLevel = -1;
function ensureThrustLoop() {
  if (thrustSrc) return true;
  const ac = ensureAudio(); if (!ac || !sfxGain) return false;
  try {
    const len = Math.floor(ac.sampleRate * 1.0);
    const buf = ac.createBuffer(1, len, ac.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    const src = ac.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    const f = ac.createBiquadFilter();
    f.type = "lowpass";
    f.frequency.value = 320;
    const g = ac.createGain();
    g.gain.value = 0;
    src.connect(f); f.connect(g); g.connect(sfxGain);
    src.start();
    thrustSrc = src; thrustGain = g; thrustFilter = f;
    return true;
  } catch (e) { return false; }
}
function setThrustSound(level) {
  if (!ensureThrustLoop()) return;
  if (Math.abs(level - thrustLevel) < 0.08) return;
  thrustLevel = level;
  const ac = actx;
  const t = ac.currentTime;
  thrustGain.gain.setTargetAtTime(Math.max(0, Math.min(1, level)) * 0.10, t, 0.09);
  thrustFilter.frequency.setTargetAtTime(280 + Math.max(0, Math.min(1, level)) * 520, t, 0.12);
}
function sfxExplosion() {
  sfxNoise(0.6, 0.3, 2200, 60);
  sfxTone(70, 0.6, "sawtooth", 0.22, 28);
}
function sfxLevelUp() {
  sfxTone(523, 0.12, "sine", 0.13);
  setTimeout(() => sfxTone(659, 0.12, "sine", 0.13), 90);
  setTimeout(() => sfxTone(784, 0.2, "sine", 0.13), 180);
}

// Player
let player = {
  pos: [10, 10, 10],      // x,y,z in 64^3 cube
  look: [0, 0],           // yaw, pitch (radians)
  hp: 100,
  shields: 100,
  max_hp: 100,
  max_shields: 100,
  xp: 0,
  name: "Player",
};

// NPCs (enemies around the portal). The roster is authoritative: it arrives
// in the welcome frame and is refreshed whenever the server resends it.
let npcs = [];

// Harvestable debris between spawn and the wormhole.
let debris = [];

// Scan chart: debris ids the local pilot has revealed, with an expiry.
let scanBlips = {};
// Mass of each scanned chunk, so the map can show how heavy it is.
let scanMass = {};
// Contacts a wingman shared with us (shorter-lived than our own scan).
let sharedBlips = {};
// Mobs that are charging a shot, so they can flash before they fire.
let mobWindup = {};

// Weight class of a chunk: mass is density x volume (D38).
function weightClass(mass) {
  if (mass >= 20) return "massive";
  if (mass >= 5) return "heavy";
  if (mass >= 1) return "solid";
  return "light";
}

// Salvage lying in the world (from `loot` frames), with its position.
let worldLoot = [];

// Other pilots, learned from authoritative pos/vitals frames. The tactical
// HUD uses these so a squad can coordinate attacks from different locations.
let otherPilots = {};

// The squadmate we have chosen to fly with (T).
let affiliate = null;

// Live bolts in flight (D59): id -> {pos:[x,y,z]}. Authoritative from the
// server's "proj" frames; the client only draws them.
let projectiles = {};

// Our mutual PVP fight partner (D55). Set from the server's "engaged" frames.
let fightTarget = null;

// Fire range learned from the welcome frame; the server still judges shots.
let fireRange = 24;

// D25: ship stores are authoritative; welcome/vitals frames fill these in.
let vitals = {
  hp: 100, shields: 100, max_hp: 100, max_shields: 100,
  energy: 100, methane: 0, metal: 0,
  max_energy: 100, max_methane: 100, max_metal: 100,
  xp: 0, level: 1, xp_next: 100, xp_base: 0, artifacts: [],
};

// Entrance wormhole at the far end and exit wormhole at spawn. Both positions
// are authoritative and arrive in `welcome` (D1/D2).
let portal = { pos: [32, 32, 58], front: "-z", aperture: 4 };
let spawnPortal = { pos: [32, 12, 6], front: "+z" };

// Authoritative positions arrive in discrete steps; render a smoothed copy
// so the camera and ship glide between them instead of visibly snapping.
let renderPos = [10, 10, 10];

// The camera trails the ship and rotates with its heading (D20 amended):
// when the ship turns to face spawn, the view faces spawn too.
// The opening shot is third person, then eases into the cockpit. V toggles.
const CAM_THIRD = 11;
let camZoom = CAM_THIRD;
let camEye = [0, 0, 0];
let camZoomTarget = CAM_THIRD;
let introZoomTimer = null;

function initCamera() {
  const dx = portal.pos[0] - spawnPortal.pos[0];
  const dz = portal.pos[2] - spawnPortal.pos[2];
  const yaw = Math.atan2(dx, dz);
  viewYaw = yaw;
  shipYaw = yaw;
  viewPitch = 0;
  shipPitch = 0;
  // Only schedule the intro on first spawn; later welcome frames leave the
  // player's chosen camera mode alone.
  if (introZoomTimer === null && camZoom === CAM_THIRD) {
    introZoomTimer = setTimeout(() => { camZoomTarget = 0; introZoomTimer = null; }, 1800);
  }
}

function toggleCamera() {
  if (introZoomTimer) { clearTimeout(introZoomTimer); introZoomTimer = null; }
  camZoomTarget = camZoomTarget > CAM_THIRD / 2 ? 0 : CAM_THIRD;
}

function toggleHelp() {
  if (!helpEl) return;
  helpEl.classList.toggle("hidden");
}
function showHelpBriefly(ms) {
  if (!helpEl) return;
  helpEl.classList.remove("hidden");
  if (ms) setTimeout(() => helpEl.classList.add("hidden"), ms);
}

// Mini-map canvas (a 2-D overlay: the cockpit carries its own tactical map)
let miniCanvas;

// Renderer visuals are data (D1): web.py injects data/lighting.json into the
// page as window.QFE_LIGHTING before this script runs. The fallback only
// matters when opening the HTML straight off disk (no server injection).
const LIGHTING = window.QFE_LIGHTING || {
  light_direction: [0.25, 0.55, -0.80],
  view_direction: [0.10, 0.20, -1.00],
  ambient: 0.35,
  diffuse: 0.75,
  specular: 0.55,
  shininess: 16.0,
  rim_strength: 0.18,
  fog_density: 0.00008,
  fog_color: [0.02, 0.02, 0.04],
  gamma: 2.2,
};

// ── sky bloom / afterglow post-pass (D53) ────────────────────────────────
// Renderer visuals are data: bloom is only a rendering effect, tuned from
// lighting.json (sky_bloom), and never touches the authoritative world. The
// render loop feeds the lit scene (opaque, stars held back) into one full-res
// FBO, blunts it with a half-size blur ping-pong, adds a single tonemapped
// halo, and redraws the live stars on top. If float textures/FBOs or this
// program can't be created the loop silently degrades to the single-pass draw.

let bloomProgram = null;   // fullscreen blur + additive composite program
let quadBuf = null;        // fullscreen tri, a_pos → v_uv, shared by the passes
let tonemapper = (LIGHTING.sky_bloom && LIGHTING.sky_bloom.tonemap_factor) || 1.0;  // one Reinhard-ish tonemap factor (from lighting.json)
let haloStrength = (LIGHTING.sky_bloom && LIGHTING.sky_bloom.halo_strength) || 0.4;  // halo gain (from lighting.json)
let brightThreshold = (LIGHTING.sky_bloom && LIGHTING.sky_bloom.bright_threshold) || 0.5; // bright-pass cutoff (from lighting.json)
let BLOOM_ON = true;       // flips to false by enableBloom()/ensureFBOs() when it fails

function enableBloom() {
  try {
    // HDR float FBOs. Querying it is cheap and tells the render loop whether to
    // keep the expensive pipeline; its absence is not fatal.
    if (typeof gl.getExtension === "function")
      gl.getExtension("OES_texture_float");
    bloomProgram = gl.createProgram();
    if (!bloomProgram) return false;
    // Fullscreen-triangle geometry shared by the blur and composite passes.
    const vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs,
      "attribute vec2 a_pos;\nvarying highp vec2 v_uv;" +
      "void main(){ v_uv = a_pos*0.5+0.5; gl_Position=vec4(a_pos,0.0,1.0);}" );
    gl.compileShader(vs);
    if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
      console.error("bloom vertex shader error:", gl.getShaderInfoLog(vs));
      bloomProgram = null; return false;
    }
    const fs = gl.createShader(gl.FRAGMENT_SHADER);
    // Single x-step bright-pass blur, then the additive halo, one pass. The
    // tap count is baked from lighting.json (GLSL ES 1.00 needs a constant).
    const stops = Math.max(1, Math.min(8,
      Math.floor((LIGHTING.sky_bloom && LIGHTING.sky_bloom.blur_stops) || 4)));
    const glslNum = (n) => {
      const s = String(n);
      return (s.indexOf(".") === -1 && s.indexOf("e") === -1) ? s + ".0" : s;
    };
    gl.shaderSource(fs,
      "precision mediump float;" +
      "uniform sampler2D u_src;" +
      "uniform float u_tonemapper, u_halo, u_threshold;" +
      "varying highp vec2 v_uv;" +
      "uniform vec2 u_shift, u_step;" +
      "void main(){" +
      "  vec2 t = v_uv - u_shift;" +
      "  float s = 0.0;" +
      "  for(int k=0;k<" + stops + ";k++){" +
      "    vec3 c = texture2D(u_src, t).rgb;" +
      "    float l = dot(c, vec3(0.299,0.587,0.114));" +
      "    s += max(l - u_threshold, 0.0);" +  // bright-pass: only bright areas spill
      "    t.x += u_step.x;}" +
      "  s *= " + glslNum(1 / stops) + ";" +
      "  vec3 base = texture2D(u_src, v_uv).rgb;" +
      "  vec3 halo = vec3(s * u_halo);" +
      "  /* One tonemap factor (from lighting.json) clamps the additive halo. */" +
      "  halo = halo / (1.0 + halo / u_tonemapper);" +  // Reinhard on the halo
      "  gl_FragColor = vec4(base + halo, 1.0);}" );
    gl.compileShader(fs);
    if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
      console.error("bloom fragment shader error:", gl.getShaderInfoLog(fs));
      bloomProgram = null; return false;
    }
    gl.attachShader(bloomProgram, vs);
    gl.attachShader(bloomProgram, fs);
    gl.linkProgram(bloomProgram);
    if (!gl.getProgramParameter(bloomProgram, gl.LINK_STATUS)) {
      console.error("bloom program link error:", gl.getProgramInfoLog(bloomProgram));
      bloomProgram = null; return false;
    }
  } catch (e) {
    console.error("bloom init failed:", e);
    bloomProgram = null;
  }
  if (!bloomProgram) BLOOM_ON = false;
  return bloomProgram !== null;
}

// Fullscreen triangle for the blur/composite passes (a_pos → v_uv), one-time.
function initQuadBuf() {
  quadBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
  // Oversized triangle: covers the whole [-1,1] viewport with one primitive.
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
    -1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
}
// ── bloom FBOs + composite (D53) ────────────────────────────────────────
// Full-res float render target for the opaque scene, plus the shared composite
// pass. Degrades to the single-pass path (BLOOM_ON=false) on any failure.
let opaqueFBO = null, opaqueTex = null, opaqueDepth = null;
let fboW = 0, fboH = 0;

function ensureFBOs(w, h) {
  if (opaqueFBO && fboW === w && fboH === h) return true;
  // dispose previous
  if (opaqueFBO) { try { gl.deleteFramebuffer(opaqueFBO); } catch (e) {} }
  if (opaqueTex) { try { gl.deleteTexture(opaqueTex); } catch (e) {} }
  if (opaqueDepth) { try { gl.deleteRenderbuffer(opaqueDepth); } catch (e) {} }
  opaqueFBO = null; opaqueTex = null; opaqueDepth = null;
  const floatExt = (typeof gl.getExtension === "function") ? gl.getExtension("OES_texture_float") : null;
  if (!floatExt) { BLOOM_ON = false; return false; }
  const rgba = gl.RGBA;
  const flt = gl.FLOAT;
  const clamp = gl.CLAMP_TO_EDGE;
  opaqueTex = gl.createTexture();
  if (!opaqueTex) { BLOOM_ON = false; return false; }
  gl.bindTexture(gl.TEXTURE_2D, opaqueTex);
  gl.texImage2D(gl.TEXTURE_2D, 0, rgba, w, h, 0, rgba, flt, null);
  // Float textures need OES_texture_float_linear for LINEAR filtering; without
  // it sampling returns black. The FBO is full-resolution and sampled 1:1, so
  // NEAREST is both correct and portable.
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, clamp);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, clamp);
  opaqueFBO = gl.createFramebuffer();
  if (!opaqueFBO) { BLOOM_ON = false; return false; }
  gl.bindFramebuffer(gl.FRAMEBUFFER, opaqueFBO);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, opaqueTex, 0);
  opaqueDepth = gl.createRenderbuffer();
  if (opaqueDepth) {
    gl.bindRenderbuffer(gl.RENDERBUFFER, opaqueDepth);
    gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, w, h);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, opaqueDepth);
  }
  if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
    BLOOM_ON = false;
    try { gl.bindFramebuffer(gl.FRAMEBUFFER, null); } catch (e) {}
    return false;
  }
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  fboW = w; fboH = h;
  return true;
}

function compositeBloom(w, h) {
  if (!bloomProgram || !quadBuf || !opaqueTex) return;
  const loc = gl.getAttribLocation(bloomProgram, "a_pos");
  gl.useProgram(bloomProgram);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  gl.viewport(0, 0, w, h);
  gl.disable(gl.DEPTH_TEST);
  gl.disable(gl.BLEND);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, opaqueTex);
  const uSrc = gl.getUniformLocation(bloomProgram, "u_src");
  const uShift = gl.getUniformLocation(bloomProgram, "u_shift");
  const uStep = gl.getUniformLocation(bloomProgram, "u_step");
  const uTonemap = gl.getUniformLocation(bloomProgram, "u_tonemapper");
  const uHalo = gl.getUniformLocation(bloomProgram, "u_halo");
  const uThreshold = gl.getUniformLocation(bloomProgram, "u_threshold");
  if (uSrc) gl.uniform1i(uSrc, 0);
  if (uShift) gl.uniform2f(uShift, 0, 0);
  const radius = (LIGHTING.sky_bloom && LIGHTING.sky_bloom.blur_radius) || 1.2;
  if (uStep) gl.uniform2f(uStep, radius / Math.max(1, w), 0);
  if (uTonemap) gl.uniform1f(uTonemap, tonemapper);
  if (uHalo) gl.uniform1f(uHalo, haloStrength);
  if (uThreshold) gl.uniform1f(uThreshold, brightThreshold);
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  gl.enable(gl.DEPTH_TEST);
  // restore lit program
  gl.useProgram(program);
}

function initGL() {
  canvas = document.querySelector("#glcanvas");
  if (!canvas) {
    console.error("No #glcanvas found");
    return;
  }
  gl = canvas.getContext("webgl", {
    antialias: true, alpha: false, depth: true, powerPreference: "high-performance",
  });
  if (!gl) {
    console.error("WebGL not supported");
    return;
  }

  // Lighting scalars are baked from JSON into GLSL literals, leaving one
  // light-direction uniform (set once below). GLSL needs a decimal point to
  // treat a number as float — "16" would be an int and break pow().
  const glslFloat = (n) => {
    const s = String(Number(n));
    return (s.indexOf(".") === -1 && s.indexOf("e") === -1) ? s + ".0" : s;
  };
  const viewDir = LIGHTING.view_direction.map(glslFloat).join(", ");
  const fogColor = LIGHTING.fog_color.map(glslFloat).join(", ");

  // Shaders — per-vertex normals, per-fragment ambient + Lambertian diffuse
  // + Blinn–Phong specular (GLSL ES 1.00, works in WebGL1 and WebGL2).
  const vsSource = `
    attribute vec3 a_position;
    attribute vec3 a_normal;
    uniform mat4 u_proj;
    uniform mat4 u_view;
    uniform mat4 u_model;
    uniform vec4 u_color;
    varying vec4 v_color;
    varying vec3 v_normal;
    varying float v_dist;
    void main() {
      v_color = u_color;
      // w = 0 ignores translation and keeps only the model's rotation/scale.
      v_normal = (u_model * vec4(a_normal, 0.0)).xyz;
      vec4 viewPos = u_view * u_model * vec4(a_position, 1.0);
      v_dist = -viewPos.z;
      gl_Position = u_proj * viewPos;
      // Only the starfield draws points; every other mesh ignores this.
      gl_PointSize = 2.0;
    }`;

  const fsSource = `
    precision mediump float;
    uniform vec3 u_lightDir;
    uniform float u_emissive;
    varying vec4 v_color;
    varying vec3 v_normal;
    varying float v_dist;
    void main() {
      vec3 N = normalize(v_normal);
      vec3 L = normalize(u_lightDir);
      vec3 V = normalize(vec3(${viewDir}));
      float ndl = max(dot(N, L), 0.0);
      vec3 H = normalize(L + V);
      float spec = ndl > 0.0
        ? pow(max(dot(N, H), 0.0), ${glslFloat(LIGHTING.shininess)})
        : 0.0;
      float rim = pow(1.0 - max(dot(N, V), 0.0), 3.0) * ${glslFloat(LIGHTING.rim_strength)};
      float light = ${glslFloat(LIGHTING.ambient)}
                  + ${glslFloat(LIGHTING.diffuse)} * ndl
                  + ${glslFloat(LIGHTING.specular)} * spec + rim;
      vec3 rgb = mix(v_color.rgb * light, v_color.rgb, u_emissive);
      // Exponential-squared distance haze, skipped for glowing surfaces.
      float fog = 1.0 - exp(-${glslFloat(LIGHTING.fog_density)} * v_dist * v_dist);
      rgb = mix(rgb, vec3(${fogColor}), clamp(fog, 0.0, 1.0) * (1.0 - u_emissive));
      // Linear-to-sRGB style gamma for a brighter, more natural image.
      rgb = pow(max(rgb, vec3(0.0)), vec3(1.0 / ${glslFloat(LIGHTING.gamma)}));
      gl_FragColor = vec4(rgb, v_color.a);
    }`;

  const vertexShader = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vertexShader, vsSource);
  gl.compileShader(vertexShader);
  if (!gl.getShaderParameter(vertexShader, gl.COMPILE_STATUS)) {
    console.error("Vertex shader error:", gl.getShaderInfoLog(vertexShader));
    return;
  }

  const fragmentShader = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fragmentShader, fsSource);
  gl.compileShader(fragmentShader);
  if (!gl.getShaderParameter(fragmentShader, gl.COMPILE_STATUS)) {
    console.error("Fragment shader error:", gl.getShaderInfoLog(fragmentShader));
    return;
  }

  program = gl.createProgram();
  gl.attachShader(program, vertexShader);
  gl.attachShader(program, fragmentShader);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.error("Program link error:", gl.getProgramInfoLog(program));
    return;
  }

  // Cache the lighting/normal attribute locations once the program is linked.
  gl.useProgram(program);
  normalLoc = gl.getAttribLocation(program, "a_normal");
  emissiveLoc = gl.getUniformLocation(program, "u_emissive");
  gl.uniform3fv(gl.getUniformLocation(program, "u_lightDir"),
                new Float32Array(LIGHTING.light_direction));
  gl.uniform1f(emissiveLoc, 0);

  projMatrix = mat4.create();
  viewMatrix = mat4.create();
  modelMatrix = mat4.create();

  // Set up projection (perspective). Mirror X so that +x (east / D) is
  // screen-right, matching the mini-map and normal map conventions.
  mat4.perspective(projMatrix, Math.PI / 3, canvas.clientWidth / canvas.clientHeight, 0.1, 1000.0);
  projMatrix[0] = -projMatrix[0];
}

// ── math helpers (4×4 matrices) ────────────────────────────────────────
// Minimal mat4 from gl-matrix.js behavior — inlined to avoid deps.
let mat4 = {
  create: () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]),
  perspective: (m, yfov, aspect, near, far) => {
    const f = 1.0 / Math.tan(yfov / 2);
    m[0] = f / aspect; m[1] = 0; m[2] = 0; m[3] = 0;
    m[4] = 0; m[5] = f; m[6] = 0; m[7] = 0;
    m[8] = 0; m[9] = 0; m[11] = -1;
    m[10] = (far + near) / (near - far);
    m[12] = 0; m[13] = 0;
    m[14] = (2 * far * near) / (near - far);
    m[15] = 0;
    return m;
  },
  lookAt: (m, eye, center, up) => {
    let x0, x1, x2, y0, y1, y2, z0, z1, z2, len;
    const eyex = eye[0], eyey = eye[1], eyez = eye[2];
    const upx = up[0], upy = up[1], upz = up[2];
    const centerx = center[0], centery = center[1], centerz = center[2];
    z0 = eyex - centerx; z1 = eyey - centery; z2 = eyez - centerz;
    len = Math.hypot(z0, z1, z2); if(!len) { len = 0.0001; }
    z0 /= len; z1 /= len; z2 /= len;
    x0 = upy * z2 - upz * z1; x1 = upz * z0 - upx * z2; x2 = upx * z1 - upy * z0;
    len = Math.hypot(x0, x1, x2); if(!len) { len = 0.0001; }
    x0 /= len; x1 /= len; x2 /= len;
    y0 = z1 * x2 - z2 * x1; y1 = z2 * x0 - z0 * x2; y2 = z0 * x1 - z1 * x0;
    len = Math.hypot(y0, y1, y2); if(!len) { len = 0.0001; }
    y0 /= len; y1 /= len; y2 /= len;
    m[0] = x0; m[1] = y0; m[2] = z0; m[3] = 0;
    m[4] = x1; m[5] = y1; m[6] = z1; m[7] = 0;
    m[8] = x2; m[9] = y2; m[10] = z2; m[11] = 0;
    m[12] = -(x0*eyex + x1*eyey + x2*eyez);
    m[13] = -(y0*eyex + y1*eyey + y2*eyez);
    m[14] = -(z0*eyex + z1*eyey + z2*eyez);
    m[15] = 1;
    return m;
  },
  translate: (m, v) => {
    const out = m.slice();
    out[12] = out[0]*v[0] + out[4]*v[1] + out[8]*v[2] + out[12];
    out[13] = out[1]*v[0] + out[5]*v[1] + out[9]*v[2] + out[13];
    out[14] = out[2]*v[0] + out[6]*v[1] + out[10]*v[2] + out[14];
    out[15] = out[3]*v[0] + out[7]*v[1] + out[11]*v[2] + out[15];
    return out;
  },
  rotateY: (m, angle) => {
    const out = m.slice();
    const c = Math.cos(angle), s = Math.sin(angle);
    out[0] = c * m[0] + s * m[2]; out[2] = c * m[2] - s * m[0];
    out[8] = c * m[8] + s * m[10]; out[10] = c * m[10] - s * m[8];
    out[4] = c * m[4] + s * m[6]; out[6] = c * m[6] - s * m[4];
    out[9] = c * m[9] + s * m[11]; out[11] = c * m[11] - s * m[9];
    return out;
  },
  rotateX: (m, angle) => {
    const out = m.slice();
    const c = Math.cos(angle), s = Math.sin(angle);
    out[5] = c * m[5] + s * m[9]; out[6] = c * m[6] + s * m[8]; out[9] = c * m[9] - s * m[5]; out[10] = c * m[10] - s * m[6];
    out[1] = c * m[1] + s * m[3]; out[2] = c * m[2] - s * m[1]; out[7] = c * m[7] - s * m[1];
    return out;
  },
  // Column-major A × B (apply B first, then A).
  multiply: (a, b) => {
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c++)
      for (let r = 0; r < 4; r++)
        out[c*4+r] = a[r]*b[c*4] + a[4+r]*b[c*4+1]
                   + a[8+r]*b[c*4+2] + a[12+r]*b[c*4+3];
    return out;
  },
  // Column-major translate × uniform scale, for placing a unit cube.
  model: (x, y, z, s) => new Float32Array([
    s, 0, 0, 0,
    0, s, 0, 0,
    0, 0, s, 0,
    x, y, z, 1,
  ]),
  // Column-major translate × rotateZ × uniform scale, for the wormhole rings.
  modelRotZ: (x, y, z, s, angle) => {
    const c = Math.cos(angle), sn = Math.sin(angle);
    return new Float32Array([
       s * c,  s * sn, 0, 0,
      -s * sn, s * c,  0, 0,
       0,      0,      s, 0,
       x,      y,      z, 1,
    ]);
  },
  // Translate × yaw (+Y) × pitch (+X) × uniform scale. Compose the two
  // rotations so local +Z points exactly along the view-forward vector.
  modelYawPitch: (x, y, z, s, yaw, pitch) => {
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    return new Float32Array([
       cy * s,        0,      -sy * s,        0,
      -sy * sp * s,   cp * s, -cy * sp * s,    0,
       sy * cp * s,   sp * s,  cy * cp * s,    0,
       x,             y,       z,              1,
    ]);
  },
  // Translate × rotateY × rotateZ × non-uniform scale, for fighter sub-parts.
  modelBox: (x, y, z, sx, sy, sz, ry = 0, rz = 0) => {
    const cy = Math.cos(ry), syaw = Math.sin(ry);
    const cz = Math.cos(rz), szroll = Math.sin(rz);
    return new Float32Array([
       cy * cz * sx,        szroll * sx,       -syaw * cz * sx,       0,
      -cy * szroll * sy,    cz * sy,            syaw * szroll * sy,   0,
       syaw * sz,           0,                  cy * sz,              0,
       x,                   y,                  z,                    1,
    ]);
  },
};

// ── per-vertex normals ─────────────────────────────────────────────────
// Each mesh keeps its existing position buffer; a parallel normal buffer of
// identical length supplies one flat (face) normal per vertex. No new draw
// path — every draw call just binds the extra attribute.
function normalsFromTriangles(positions) {
  const out = new Array(positions.length);
  for (let i = 0; i < positions.length; i += 9) {
    const ux = positions[i + 3] - positions[i];
    const uy = positions[i + 4] - positions[i + 1];
    const uz = positions[i + 5] - positions[i + 2];
    const vx = positions[i + 6] - positions[i];
    const vy = positions[i + 7] - positions[i + 1];
    const vz = positions[i + 8] - positions[i + 2];
    let nx = uy * vz - uz * vy;
    let ny = uz * vx - ux * vz;
    let nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    for (let k = 0; k < 9; k += 3) {
      out[i + k] = nx; out[i + k + 1] = ny; out[i + k + 2] = nz;
    }
  }
  return out;
}

function makeNormalBuffer(data) {
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
  return buf;
}

function bindNormals(buffer) {
  if (normalLoc < 0) return;
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.enableVertexAttribArray(normalLoc);
  gl.vertexAttribPointer(normalLoc, 3, gl.FLOAT, false, 0, 0);
}

// ── draw a cube ────────────────────────────────────────────────────────
let cubeBuffer;
let cubeNormalBuffer;

function initCube() {
  // 36 vertices (12 triangles × 3) for a unit cube centered at the origin.
  const positions = [
    // front (z =  1)
    -1, -1,  1,   1, -1,  1,   1,  1,  1,
    -1, -1,  1,   1,  1,  1,  -1,  1,  1,
    // back (z = -1)
    -1, -1, -1,  -1,  1, -1,   1,  1, -1,
    -1, -1, -1,   1,  1, -1,   1, -1, -1,
    // top (y =  1)
    -1,  1, -1,  -1,  1,  1,   1,  1,  1,
    -1,  1, -1,   1,  1,  1,   1,  1, -1,
    // bottom (y = -1) — wound like the other faces so the cross-product
    // normal points outward (down) instead of inward.
    -1, -1,  1,   1, -1, -1,   1, -1,  1,
    -1, -1,  1,  -1, -1, -1,   1, -1, -1,
    // right (x =  1)
     1, -1, -1,   1,  1, -1,   1,  1,  1,
     1, -1, -1,   1,  1,  1,   1, -1,  1,
    // left (x = -1)
    -1, -1,  1,  -1,  1,  1,  -1,  1, -1,
    -1, -1,  1,  -1,  1, -1,  -1, -1, -1,
  ];

  cubeBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, cubeBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(positions), gl.STATIC_DRAW);
  cubeNormalBuffer = makeNormalBuffer(normalsFromTriangles(positions));
}

// ── wormhole ring geometry ──────────────────────────────────────────────
// A flat annulus in the XY plane (facing +Z). Alternating segments are
// grouped so we can tint them differently and make the spin visible.
let wormholeBuffer;
let wormholeNormalBuffer;
let wormholeSegCount = 0; // vertices per alternating group

// ── player fighter ─────────────────────────────────────────────────────
// Low-poly interpretation of the reference art: swept delta wings, twin
// canted tail fins, a ridged armoured fuselage and glowing engine nozzles.
// Local +Z is the nose, +Y is up, +X is starboard.
const fighterBoxes = [
  { p: [0, 0, 0],          h: [0.45, 0.28, 1.80], c: [0.36, 0.42, 0.47] }, // hull
  { p: [0, -0.02, 2.05],   h: [0.20, 0.16, 0.65], c: [0.33, 0.38, 0.42] }, // nose
  { p: [0, 0.32, 0.25],    h: [0.18, 0.14, 1.30], c: [0.30, 0.35, 0.39] }, // spine
  { p: [0, 0.40, 1.05],    h: [0.20, 0.13, 0.55], c: [0.09, 0.19, 0.25] }, // canopy
  { p: [-1.05, -0.18, -1.15], h: [0.32, 0.26, 0.95], c: [0.27, 0.31, 0.35] }, // port pod
  { p: [ 1.05, -0.18, -1.15], h: [0.32, 0.26, 0.95], c: [0.27, 0.31, 0.35] }, // starboard pod
  { p: [-0.95, 0.95, -1.15], h: [0.09, 0.85, 0.42], c: [0.31, 0.36, 0.40], rz:  0.22 },
  { p: [ 0.95, 0.95, -1.15], h: [0.09, 0.85, 0.42], c: [0.31, 0.36, 0.40], rz: -0.22 },
  { p: [-1.35, 0.09, 0.10], h: [0.55, 0.02, 0.05], c: [0.24, 0.28, 0.31] }, // wing panels
  { p: [ 1.35, 0.09, 0.10], h: [0.55, 0.02, 0.05], c: [0.24, 0.28, 0.31] },
];
const fighterGlow = [
  { p: [-1.05, -0.18, -2.18], h: [0.22, 0.16, 0.05], c: [1.0, 0.34, 0.05, 0.40] },
  { p: [ 1.05, -0.18, -2.18], h: [0.22, 0.16, 0.05], c: [1.0, 0.34, 0.05, 0.40] },
];
// Tiny nose-tip manoeuvring jets: underside fires when climbing, topside
// when descending (D25). Alpha is driven by verticalThrust in drawFighter.
const fighterManeuver = [
  { p: [0, -0.26, 2.50], h: [0.10, 0.22, 0.12], c: [0.35, 0.85, 1.0, 0] },
  { p: [0,  0.26, 2.50], h: [0.10, 0.22, 0.12], c: [0.35, 0.85, 1.0, 0] },
];

// Canopy: angular struts at the edges, dark metal with an emissive amber
// strip, so the view stays mostly open (D27) but reads as a real cockpit.
const cockpitFrame = [
  { p: [ 0,    1.12, 2.20], h: [0.92, 0.018, 0.018], c: [0.10, 0.11, 0.13] }, // top rim
  { p: [-0.91, 0.70, 2.20], h: [0.018, 0.42, 0.018], c: [0.10, 0.11, 0.13] }, // port
  { p: [ 0.91, 0.70, 2.20], h: [0.018, 0.42, 0.018], c: [0.10, 0.11, 0.13] }, // starboard
  { p: [ 0,    0.28, 2.20], h: [0.92, 0.018, 0.018], c: [0.10, 0.11, 0.13] }, // lower rim
  // Angled corner struts, like the reference cockpit.
  { p: [-1.02, 1.02, 2.20], h: [0.035, 0.32, 0.035], c: [0.10, 0.11, 0.13], rz: -0.62 },
  { p: [ 1.02, 1.02, 2.20], h: [0.035, 0.32, 0.035], c: [0.10, 0.11, 0.13], rz:  0.62 },
  { p: [-1.02, 0.10, 2.20], h: [0.030, 0.26, 0.030], c: [0.10, 0.11, 0.13], rz:  0.58 },
  { p: [ 1.02, 0.10, 2.20], h: [0.030, 0.26, 0.030], c: [0.10, 0.11, 0.13], rz: -0.58 },
];
const cockpitGlow = [
  // Firing crosshair: four short bars around the centre of the view.
  { p: [ 0, 0.80, 2.25], h: [0.09, 0.006, 0.006], c: [0.28, 0.74, 1.00, 0.85] },
  { p: [ 0, 0.94, 2.25], h: [0.09, 0.006, 0.006], c: [0.28, 0.74, 1.00, 0.85] },
  { p: [-0.06, 0.87, 2.25], h: [0.006, 0.05, 0.006], c: [0.28, 0.74, 1.00, 0.85] },
  { p: [ 0.06, 0.87, 2.25], h: [0.006, 0.05, 0.006], c: [0.28, 0.74, 1.00, 0.85] },
];

let fighterWingBuffer;
let fighterWingNormalBuffer;
let fighterWingFaceCount = 0;
let fighterWingEdgeCount = 0;

function initFighter() {
  const faces = [], edges = [];
  for (const sign of [-1, 1]) {
    // Swept delta wing outline in the XZ plane (root-front -> tip -> trailing).
    const wing = [
      [sign * 0.36,  1.25],
      [sign * 2.55, -0.30],
      [sign * 2.05, -1.15],
      [sign * 0.40, -1.35],
    ];
    const top = 0.06, bottom = -0.06;
    // Mirroring the outline flips its winding; reverse the port (-x) wing's
    // triangles so every face normal comes out pointing away from the mesh.
    const tri = (arr, a, b, c) => {
      if (sign < 0) arr.push(...c, ...b, ...a);
      else arr.push(...a, ...b, ...c);
    };
    tri(faces, [wing[0][0], top, wing[0][1]], [wing[1][0], top, wing[1][1]], [wing[2][0], top, wing[2][1]]);
    tri(faces, [wing[0][0], top, wing[0][1]], [wing[2][0], top, wing[2][1]], [wing[3][0], top, wing[3][1]]);
    tri(faces, [wing[0][0], bottom, wing[0][1]], [wing[2][0], bottom, wing[2][1]], [wing[1][0], bottom, wing[1][1]]);
    tri(faces, [wing[0][0], bottom, wing[0][1]], [wing[3][0], bottom, wing[3][1]], [wing[2][0], bottom, wing[2][1]]);
    for (let i = 0; i < 4; i++) {
      const a = wing[i], b = wing[(i + 1) % 4];
      tri(edges, [a[0], top, a[1]], [b[0], bottom, b[1]], [b[0], top, b[1]]);
      tri(edges, [a[0], top, a[1]], [a[0], bottom, a[1]], [b[0], bottom, b[1]]);
    }
  }
  fighterWingFaceCount = faces.length / 3;
  fighterWingEdgeCount = edges.length / 3;
  const wingPositions = [...faces, ...edges];
  fighterWingBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, fighterWingBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(wingPositions), gl.STATIC_DRAW);
  fighterWingNormalBuffer = makeNormalBuffer(normalsFromTriangles(wingPositions));
}

// ── rear thruster plume (D25) ───────────────────────────────────────────
// Two small emissive cones behind the nozzles; their length and brightness
// follow the short puff fired by every commanded move.
let thrustBuffer, thrustNormalBuffer, thrustVertexCount;

function initThrust() {
  const plume = [];
  const y = -0.18, z0 = -2.25, len = 1.05, radius = 0.13;
  for (const cx of [-1.05, 1.05]) {
    const tip = [cx, y, z0 - len];
    for (let k = 0; k < 6; k++) {
      const a0 = k * Math.PI / 3;
      const a1 = (k + 1) * Math.PI / 3;
      plume.push(
        cx + Math.cos(a0) * radius, y + Math.sin(a0) * radius, z0,
        cx + Math.cos(a1) * radius, y + Math.sin(a1) * radius, z0,
        tip[0], tip[1], tip[2],
      );
    }
  }
  thrustBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, thrustBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(plume), gl.STATIC_DRAW);
  thrustNormalBuffer = makeNormalBuffer(normalsFromTriangles(plume));
  thrustVertexCount = plume.length / 3;
}

// Shared lit/emplaced box drawing for the ship and its cockpit instruments.
function drawBoxPart(placement, part, emissive = 0) {
  const loc = gl.getAttribLocation(program, "a_position");
  const uModel = gl.getUniformLocation(program, "u_model");
  const uColor = gl.getUniformLocation(program, "u_color");
  const local = mat4.modelBox(part.p[0], part.p[1], part.p[2],
                              part.h[0], part.h[1], part.h[2],
                              part.ry || 0, part.rz || 0);
  gl.uniformMatrix4fv(uModel, false, mat4.multiply(placement, local));
  // Box colours are authored as RGB; u_color is a vec4. Default rather than
  // throw if a part was authored without a colour.
  const c = part.c || [1, 1, 1, 1];
  gl.uniform4fv(uColor, c.length === 4 ? c : [...c, 1]);
  gl.uniform1f(emissiveLoc, emissive);
  gl.bindBuffer(gl.ARRAY_BUFFER, cubeBuffer);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  bindNormals(cubeNormalBuffer);
  gl.drawArrays(gl.TRIANGLES, 0, 36);
}

function drawFighter(x, y, z, scale, yaw = 0, pitch = 0) {
  if (!cubeBuffer || !fighterWingBuffer) return;
  const loc = gl.getAttribLocation(program, "a_position");
  const uModel = gl.getUniformLocation(program, "u_model");
  const uColor = gl.getUniformLocation(program, "u_color");
  const placement = mat4.modelYawPitch(x, y, z, scale, yaw, pitch);

  // Box sub-parts are authored in ship-local space, then placed in the world.
  const drawPart = (part, emissive = 0) => drawBoxPart(placement, part, emissive);

  for (const part of fighterBoxes) drawPart(part);

  // Muzzle flash: brief bright puff at the nose while the gun is hot.
  if (cubeBuffer && nowMs() < fireUntil) {
    const flash = { p: [0, 0, 2.0], h: [0.45, 0.45, 0.35], c: [1.0, 0.55, 0.15, 1] };
    drawBoxPart(placement, flash, 1);
  }

  gl.uniformMatrix4fv(uModel, false, placement);
  gl.uniform1f(emissiveLoc, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, fighterWingBuffer);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  bindNormals(fighterWingNormalBuffer);
  gl.uniform4fv(uColor, [0.34, 0.40, 0.45, 1]);           // lit wing faces
  gl.drawArrays(gl.TRIANGLES, 0, fighterWingFaceCount);
  gl.uniform4fv(uColor, [0.22, 0.26, 0.30, 1]);           // shaded wing edges
  gl.drawArrays(gl.TRIANGLES, fighterWingFaceCount, fighterWingEdgeCount);

  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
  // index 0 = port (left) engine, index 1 = starboard (right) engine.
  for (let i = 0; i < fighterGlow.length; i++) {
    const base = fighterGlow[i].c;
    const f = i === 0 ? 1 - engineBias * 0.55 : 1 + engineBias * 0.55;
    drawPart({ ...fighterGlow[i],
               c: [base[0] * f, base[1] * f, base[2] * f, Math.min(1, base[3] * f)] }, 1);
  }

  if (thrust > 0.01 && thrustBuffer) {
    // Scale the plume along Z about the nozzle plane so the base stays put.
    const k = 0.35 + 0.65 * thrust;
    const stretch = new Float32Array([
      1, 0, 0, 0,
      0, 1, 0, 0,
      0, 0, k, 0,
      0, 0, -2.25 * (1 - k), 1,
    ]);
    gl.uniformMatrix4fv(uModel, false, mat4.multiply(placement, stretch));
    gl.uniform4fv(uColor, [1.0, 0.45, 0.10, 0.85 * thrust]);
    gl.bindBuffer(gl.ARRAY_BUFFER, thrustBuffer);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
    bindNormals(thrustNormalBuffer);
    gl.uniform1f(emissiveLoc, 1);
    gl.drawArrays(gl.TRIANGLES, 0, thrustVertexCount);
  }

  if (Math.abs(verticalThrust) > 0.01) {
    // Underside jet for climbing, topside jet for descending.
    for (let i = 0; i < fighterManeuver.length; i++) {
      const power = i === 0 ? Math.max(0, verticalThrust) : Math.max(0, -verticalThrust);
      if (power <= 0) continue;
      const base = fighterManeuver[i].c;
      drawPart({ ...fighterManeuver[i],
                 c: [base[0], base[1], base[2], 0.8 * power] }, 1);
    }
  }

  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.BLEND);
  gl.uniform1f(emissiveLoc, 0);
}

// ── cockpit window frame ─────────────────────────────────────────────────
// Only a thin canopy rim and a small gunsight are drawn in 3-D; all readouts
// live in the fixed-zone HTML HUD so most of the view is space (D20/D27).
function drawCockpit(placement) {
  // Canopy rim removed for now (D54 visual pass): only the gunsight crosshair
  // remains so the pilot knows where shots land.
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
  for (const part of cockpitGlow) drawBoxPart(placement, part, 1);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.BLEND);
  gl.uniform1f(emissiveLoc, 0);
}

function initWormhole() {
  const segments = 48;
  const inner = 0.65, outer = 1.0;
  const even = [], odd = [];
  for (let i = 0; i < segments; i++) {
    const a0 = (i / segments) * Math.PI * 2;
    const a1 = ((i + 1) / segments) * Math.PI * 2;
    const i0 = [inner * Math.cos(a0), inner * Math.sin(a0), 0];
    const o0 = [outer * Math.cos(a0), outer * Math.sin(a0), 0];
    const i1 = [inner * Math.cos(a1), inner * Math.sin(a1), 0];
    const o1 = [outer * Math.cos(a1), outer * Math.sin(a1), 0];
    const target = (i % 2 === 0) ? even : odd;
    target.push(...i0, ...o0, ...o1, ...i0, ...o1, ...i1);
  }
  wormholeSegCount = even.length / 3;
  const data = new Float32Array([...even, ...odd]);
  wormholeBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, wormholeBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
  // Ring lies in XY; it is emissive so the exact normal is cosmetic, but the
  // attribute still needs one entry per vertex.
  const normals = [];
  for (let i = 0; i < data.length; i += 3) normals.push(0, 0, -1);
  wormholeNormalBuffer = makeNormalBuffer(normals);
}

// Keep the drawing buffer in sync with the CSS size.
function resize() {
  // Render at device resolution (capped) for crisp geometry on modern
  // displays; CSS keeps the logical size unchanged.
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  const w = Math.max(1, Math.floor(cssW * dpr));
  const h = Math.max(1, Math.floor(cssH * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  mat4.perspective(projMatrix, Math.PI / 3, (w / h) || 1, 0.1, 1000.0);
  projMatrix[0] = -projMatrix[0]; // keep +x screen-right after every resize
}

// ── draw scene ─────────────────────────────────────────────────────────

function drawProjectiles() {
  if (!cubeBuffer) return;
  const placement = mat4.model(0, 0, 0, 1);
  for (const s of Object.values(projectiles)) {
    if (!s || !s.pos) continue;
    const p = s.pos;
    const part = { p: [p[0], p[1], p[2]], h: [0.22, 0.22, 0.85],
                   c: [1.0, 0.85, 0.35, 1] };
    drawBoxPart(placement, part, 1);
  }
}

function drawOpaque() {
  // Far end = cool entrance, spawn = warm exit. Both spin in opposite
  // directions so a pilot can tell them apart at a glance.
  drawWormhole(portal.pos[0], portal.pos[1], portal.pos[2], 5, false);
  drawWormhole(spawnPortal.pos[0], spawnPortal.pos[1], spawnPortal.pos[2], 3, true);

  // Draw the harvestable debris field between spawn and the wormhole
  drawDebris();
  // Travelling shots (D59): bolts in flight, drawn emissive so they read
  // against the void and pick up the bloom halo.
  drawProjectiles();

  // Once the camera is in the cockpit the hull is behind us (or would be
  // inside the near plane), so stop drawing the ship.
  if (camZoom > 2.5) {
    drawFighter(player.pos[0], player.pos[1] + 1.2, player.pos[2], 1.25, shipYaw, shipPitch);
  } else {
    drawCockpit(mat4.modelYawPitch(player.pos[0], player.pos[1] + 1.2,
                                   player.pos[2], 1.25, shipYaw, shipPitch));
  }

  // Draw NPCs (enemies) as red cubes around portal, with a health bar above
  // each so the pilot can read how close a kill is.
  for (let npc of npcs) {
    if (npc.pos && occludedByRock(npc.pos[0], npc.pos[1], npc.pos[2])) continue;
    // A dark hull with a glowing core: the enemies must not read like the
    // debris they fly among.
    drawCube(npc.pos[0], npc.pos[1], npc.pos[2], 1.6, [0.42, 0.07, 0.09, 0.95]);
    const charging = (mobWindup[npc.id] || 0) > nowMs();
    drawBar(npc.pos[0], npc.pos[1], npc.pos[2], 0.62, 0.62, 0.62,
            charging ? [1.0, 0.85, 0.35, 1] : [1.0, 0.42, 0.12, 1]);
    const maxHp = npc.max_hp || 20;
    const frac = Math.max(0, Math.min(1, (npc.hp === undefined ? maxHp : npc.hp) / maxHp));
    const w = 2.0;
    const y = npc.pos[1] + 1.6;
    drawBar(npc.pos[0], y, npc.pos[2], w, 0.12, 0.12, [0.08, 0.10, 0.12, 0.85]);
    if (frac > 0) {
      const fill = frac > 0.5 ? [0.35, 0.95, 0.45, 0.95]
                 : frac > 0.22 ? [1.0, 0.82, 0.25, 0.95]
                               : [1.0, 0.28, 0.25, 0.95];
      drawBar(npc.pos[0] - (w * (1 - frac)) / 2, y, npc.pos[2],
              w * frac, 0.16, 0.16, fill);
    }
  }
}

function drawScene() {
  resize();
  gl.useProgram(program);

  // Set projection
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_proj"),
    false, projMatrix);

  // The ship follows the view directly: the nose, the crosshair and the
  // firing cone all share one orientation, so moving the mouse re-aims the
  // craft (not just the camera).
  shipYaw = viewYaw;
  shipPitch = viewPitch;
  const frameNow = nowMs();
  thrust = Math.max(0, Math.min(1, (thrustUntil - frameNow) / 180));
  setThrustSound(thrust);
  engineBias = turnBiasTarget * Math.max(0, Math.min(1, (biasUntil - frameNow) / 400));
  verticalThrust = verticalTarget * Math.max(0, Math.min(1, (vertUntil - frameNow) / 180));

  // Camera eases from third person into the pilot's seat. t=1 is the opening
  // chase view, t=0 puts the eye at the cockpit inside the hull (D20/D27).
  camZoom += (camZoomTarget - camZoom) * 0.06;
  if (Math.abs(camZoomTarget - camZoom) < 0.02) camZoom = camZoomTarget;
  const t = camZoom / CAM_THIRD;
  const sy = Math.sin(shipYaw), cy = Math.cos(shipYaw);
  const sp = Math.sin(shipPitch), cp = Math.cos(shipPitch);
  const f = [sy * cp, sp, cy * cp];
  const up = [-sy * sp, cp, -cy * sp];
  // Glide toward the latest authoritative position between network updates.
  for (let i = 0; i < 3; i++) renderPos[i] += (player.pos[i] - renderPos[i]) * 0.25;
  const shipCenter = [renderPos[0], renderPos[1] + 1.2, renderPos[2]];
  const seat = 1 - t; // 0 = third person, 1 = fully in the cockpit
  const eye = [0, 1, 2].map((i) =>
    shipCenter[i] + f[i] * (-11 * t - 0.5 * seat)
    + (i === 1 ? 1.5 * t : 0) + up[i] * 0.65 * seat);
  const focusDist = 3 + 17 * seat;
  const focus = [0, 1, 2].map((i) => shipCenter[i] + f[i] * focusDist);
  const shakeT = Math.max(0, Math.min(1, (shakeUntil - frameNow) / 180));
  const shakeAmp = shakeT * shakeMag;
  if (shakeAmp > 0.001) {
    eye[0] += (Math.random() * 2 - 1) * shakeAmp * 6;
    eye[1] += (Math.random() * 2 - 1) * shakeAmp * 3;
    eye[2] += (Math.random() * 2 - 1) * shakeAmp * 6;
  }
  camEye = eye.slice();
  mat4.lookAt(viewMatrix, eye, focus, [0, 1, 0]);
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_view"),
    false, viewMatrix);

  // Clear
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0.02, 0.02, 0.04, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);

  const cw = canvas.width, ch = canvas.height;

  if (BLOOM_ON) {
    if (!opaqueFBO && !ensureFBOs(cw, ch)) {
      // FBO creation failed → fall back to single-pass this frame.
    }
  }

  if (BLOOM_ON && opaqueFBO) {
    // 1. Opaque scene (stars excluded) into the float render FBO.
    gl.bindFramebuffer(gl.FRAMEBUFFER, opaqueFBO);
    gl.viewport(0, 0, cw, ch);
    gl.clearColor(0.02, 0.02, 0.04, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    drawOpaque();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);

    // 2. Composite: x-step blur + Reinhard tonemap → default frame.
    compositeBloom(cw, ch);

    // 3. Live stars redrawn as the final additive pass.
    drawStars();
  } else {
    // Single-pass path: stars first, then opaque body.
    drawStars();
    drawOpaque();
  }

  // Draw mini-map overlay (separate pass)
  drawMiniMap();
  updateCockpitHUD();
}

// ── wormhole portal ────────────────────────────────────────────────────
// Three concentric rings of alternating segments, each spinning at a
// different speed. The entrance is cyan/violet; the spawn exit is warm
// amber and counter-spinning so the two ends of the trip look distinct.
function drawWormhole(x, y, z, radius, exit = false) {
  if (!wormholeBuffer) return;
  const t = (typeof performance !== "undefined" ? performance.now() : Date.now()) / 1000;
  const loc = gl.getAttribLocation(program, "a_position");
  const uModel = gl.getUniformLocation(program, "u_model");
  const uColor = gl.getUniformLocation(program, "u_color");

  gl.uniform1f(emissiveLoc, 1); // glow layers keep their authored colour
  gl.depthMask(false); // let the glow layers blend without occluding each other
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE); // additive glow
  gl.bindBuffer(gl.ARRAY_BUFFER, wormholeBuffer);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  bindNormals(wormholeNormalBuffer);

  const entrance = [
    { scale: 1.30, speed: 0.35, a: [0.35, 0.10, 0.85, 0.45], b: [0.10, 0.35, 0.75, 0.40] },
    { scale: 0.95, speed: -0.60, a: [0.20, 0.85, 1.00, 0.75], b: [0.45, 0.25, 0.95, 0.60] },
    { scale: 0.62, speed: 0.90, a: [0.75, 1.00, 1.00, 0.90], b: [0.20, 0.70, 1.00, 0.80] },
  ];
  const exitLayer = [
    { scale: 1.30, speed: -0.35, a: [0.85, 0.28, 0.06, 0.45], b: [0.75, 0.12, 0.30, 0.40] },
    { scale: 0.95, speed: 0.60, a: [1.00, 0.62, 0.12, 0.75], b: [0.95, 0.35, 0.10, 0.60] },
    { scale: 0.62, speed: -0.90, a: [1.00, 0.90, 0.55, 0.90], b: [1.00, 0.70, 0.20, 0.80] },
  ];
  const layers = exit ? exitLayer : entrance;
  for (const layer of layers) {
    const angle = t * layer.speed;
    gl.uniformMatrix4fv(uModel, false, mat4.modelRotZ(x, y, z, radius * layer.scale, angle));
    gl.uniform4fv(uColor, layer.a);
    gl.drawArrays(gl.TRIANGLES, 0, wormholeSegCount);
    gl.uniform4fv(uColor, layer.b);
    gl.drawArrays(gl.TRIANGLES, wormholeSegCount, wormholeSegCount);
  }

  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.BLEND);
  gl.depthMask(true);
  gl.uniform1f(emissiveLoc, 0);
}

// ── simple cube ────────────────────────────────────────────────────────
// A flat, world-aligned box (used for enemy health bars).
function drawBar(x, y, z, sx, sy, sz, color) {
  if (!cubeBuffer) return;
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_model"), false,
    mat4.modelBox(x, y, z, sx, sy, sz));
  gl.uniform4fv(gl.getUniformLocation(program, "u_color"), color);
  gl.bindBuffer(gl.ARRAY_BUFFER, cubeBuffer);
  const loc = gl.getAttribLocation(program, "a_position");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  bindNormals(cubeNormalBuffer);
  gl.uniform1f(emissiveLoc, 1); // bars stay readable regardless of lighting
  gl.drawArrays(gl.TRIANGLES, 0, 36);
  gl.uniform1f(emissiveLoc, 0);
}

function drawCube(x, y, z, size, color) {
  if (!cubeBuffer) return;
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_model"), false,
    mat4.model(x, y, z, size / 2));
  gl.uniform4fv(gl.getUniformLocation(program, "u_color"), color);

  gl.bindBuffer(gl.ARRAY_BUFFER, cubeBuffer);
  const loc = gl.getAttribLocation(program, "a_position");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  bindNormals(cubeNormalBuffer);
  gl.uniform1f(emissiveLoc, 0);

  gl.drawArrays(gl.TRIANGLES, 0, 36);
}

// ── starfield ──────────────────────────────────────────────────────────
// A fixed sphere of points far outside the cube. It gives the void depth
// and costs one draw call. Deterministic, so every client sees the same sky.
// Each star carries a per-star twist (phase) and twinkle; the buffer is a
// single interleaved [x,y,z,weight] array (D53). The Milky Way band is a
// slab through the sphere's mid-plane, tuned from lighting.json (sky_bloom).
let starBuffer = null, starPhaseBuffer = null, starCount = 0;
let starsProgram = null;

function initStarsProgram() {
  try {
    const vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs,
      "attribute vec4 a_pos;" +
      "attribute float a_phase;" +
      "uniform mat4 u_proj, u_view, u_model;" +
      "uniform float u_time, u_twist, u_twinkle;" +
      "varying float v_alpha;" +
      "void main(){" +
      "  gl_Position = u_proj * u_view * u_model * vec4(a_pos.xyz, 1.0);" +
      "  float tw = 0.5 + 0.5 * sin(a_phase * 6.28318 + u_time * (0.6 + a_phase * 0.8));" +
      "  gl_PointSize = (1.5 + 2.0 * a_pos.w) * (0.7 + 0.3 * tw);" +
      "  v_alpha = 0.55 + 0.45 * tw;" +
      "}");
    gl.compileShader(vs);
    if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
      console.error("stars vertex shader error:", gl.getShaderInfoLog(vs));
      return false;
    }
    const fs = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fs,
      "precision mediump float;" +
      "varying float v_alpha;" +
      "void main(){" +
      "  vec2 c = gl_PointCoord - vec2(0.5);" +
      "  float d = dot(c, c);" +
      "  if (d > 0.25) discard;" +
      "  float a = (1.0 - d * 4.0) * v_alpha;" +
      "  gl_FragColor = vec4(0.82, 0.88, 1.0, a);" +
      "}");
    gl.compileShader(fs);
    if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
      console.error("stars fragment shader error:", gl.getShaderInfoLog(fs));
      return false;
    }
    starsProgram = gl.createProgram();
    gl.attachShader(starsProgram, vs);
    gl.attachShader(starsProgram, fs);
    gl.linkProgram(starsProgram);
    if (!gl.getProgramParameter(starsProgram, gl.LINK_STATUS)) {
      console.error("stars program link error:", gl.getProgramInfoLog(starsProgram));
      starsProgram = null;
      return false;
    }
    return true;
  } catch (e) {
    console.error("stars program init failed:", e);
    starsProgram = null;
    return false;
  }
}

function initStars() {
  let seed = 20260921;
  const rnd = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const N = 420;
  const pts = [];        // [x,y,z,weight] interleaved
  const phases = [];     // one float per star (twinkle phase)
  const band = (LIGHTING.sky_bloom && LIGHTING.sky_bloom.milkyway_band) || 0.45;
  for (let i = 0; i < N; i++) {
    const u = rnd() * 2 - 1;
    const theta = rnd() * Math.PI * 2;
    const s = Math.sqrt(Math.max(0, 1 - u * u));
    const r = 280 + rnd() * 130;
    const x = s * Math.cos(theta) * r;
    const y = u * r;
    const z = s * Math.sin(theta) * r;
    // Milky Way band: stars near the mid-plane (|u| <= band) get higher weight.
    const inBand = Math.abs(u) <= band;
    const weight = inBand ? (0.6 + rnd() * 0.4) : (0.15 + rnd() * 0.25);
    pts.push(x, y, z, weight);
    phases.push(rnd());
  }
  starCount = N;
  starBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, starBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.STATIC_DRAW);
  starPhaseBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, starPhaseBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(phases), gl.STATIC_DRAW);
  initStarsProgram();
}

function drawStars() {
  if (!starBuffer || !starPhaseBuffer || starCount === 0) return;
  if (!starsProgram && !initStarsProgram()) return;
  const locPos = gl.getAttribLocation(starsProgram, "a_pos");
  const locPhase = gl.getAttribLocation(starsProgram, "a_phase");
  gl.useProgram(starsProgram);
  gl.uniformMatrix4fv(gl.getUniformLocation(starsProgram, "u_proj"), false, projMatrix);
  gl.uniformMatrix4fv(gl.getUniformLocation(starsProgram, "u_view"), false, viewMatrix);
  gl.uniformMatrix4fv(gl.getUniformLocation(starsProgram, "u_model"), false, mat4.model(0, 0, 0, 1));
  gl.uniform1f(gl.getUniformLocation(starsProgram, "u_time"), nowMs() / 1000);
  gl.uniform1f(gl.getUniformLocation(starsProgram, "u_twist"), 1.0);
  gl.uniform1f(gl.getUniformLocation(starsProgram, "u_twinkle"), 1.0);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE); // additive
  gl.depthMask(false);
  gl.bindBuffer(gl.ARRAY_BUFFER, starBuffer);
  gl.enableVertexAttribArray(locPos);
  gl.vertexAttribPointer(locPos, 4, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, starPhaseBuffer);
  gl.enableVertexAttribArray(locPhase);
  gl.vertexAttribPointer(locPhase, 1, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.POINTS, 0, starCount);
  gl.depthMask(true);
  gl.disable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  // restore lit program
  gl.useProgram(program);
}

// ── asteroids: lumpy rock + crystal deposits (D25) ──────────────────────
// One mesh, tinted per type. The rock body and the crystals are separate
// buffers so each can carry its own colour; both reuse the D24 lit shader.
let rockBuffer, rockNormalBuffer, rockVertexCount;
let crystalBuffer, crystalNormalBuffer, crystalVertexCount;

function initAsteroid() {
  // Deterministic jitter: every client renders the same rock.
  let seed = 9137;
  const rnd = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };

  // Bit 0 = +x, bit 1 = +y, bit 2 = +z.
  const corner = [];
  for (let i = 0; i < 8; i++) {
    corner.push([
      (i & 1 ? 1 : -1) * (0.78 + rnd() * 0.36),
      (i & 2 ? 1 : -1) * (0.74 + rnd() * 0.38),
      (i & 4 ? 1 : -1) * (0.80 + rnd() * 0.34),
    ]);
  }
  const rock = [];
  const quad = (a, b, c, d) => rock.push(...a, ...b, ...c, ...a, ...c, ...d);
  // Outward winding on all six faces so normalsFromTriangles points out.
  quad(corner[0], corner[2], corner[3], corner[1]); // -z
  quad(corner[4], corner[5], corner[7], corner[6]); // +z
  quad(corner[0], corner[1], corner[5], corner[4]); // -y
  quad(corner[2], corner[6], corner[7], corner[3]); // +y
  quad(corner[0], corner[4], corner[6], corner[2]); // -x
  quad(corner[1], corner[3], corner[7], corner[5]); // +x

  const crystals = [];
  const spike = (center, n, r, h) => {
    const up = Math.abs(n[1]) > 0.9 ? [1, 0, 0] : [0, 1, 0];
    let u = [
      up[1] * n[2] - up[2] * n[1],
      up[2] * n[0] - up[0] * n[2],
      up[0] * n[1] - up[1] * n[0],
    ];
    const ul = Math.hypot(u[0], u[1], u[2]) || 1;
    u = [u[0] / ul, u[1] / ul, u[2] / ul];
    const v = [
      n[1] * u[2] - n[2] * u[1],
      n[2] * u[0] - n[0] * u[2],
      n[0] * u[1] - n[1] * u[0],
    ];
    const ring = [];
    for (let k = 0; k < 3; k++) {
      const a = k * Math.PI * 2 / 3;
      const ca = Math.cos(a) * r, sa = Math.sin(a) * r;
      ring.push([
        center[0] + u[0] * ca + v[0] * sa,
        center[1] + u[1] * ca + v[1] * sa,
        center[2] + u[2] * ca + v[2] * sa,
      ]);
    }
    const tip = [center[0] + n[0] * h, center[1] + n[1] * h, center[2] + n[2] * h];
    crystals.push(...ring[0], ...ring[1], ...tip,
                  ...ring[1], ...ring[2], ...tip,
                  ...ring[2], ...ring[0], ...tip,
                  ...ring[2], ...ring[1], ...ring[0]);
  };

  const faces = [
    { idx: [0, 2, 3, 1], n: [0, 0, -1] },
    { idx: [4, 5, 7, 6], n: [0, 0, 1] },
    { idx: [0, 1, 5, 4], n: [0, -1, 0] },
    { idx: [2, 6, 7, 3], n: [0, 1, 0] },
    { idx: [0, 4, 6, 2], n: [-1, 0, 0] },
    { idx: [1, 3, 7, 5], n: [1, 0, 0] },
  ];
  for (const f of faces) {
    const center = [0, 0, 0];
    for (const i of f.idx) {
      center[0] += corner[i][0] / 4;
      center[1] += corner[i][1] / 4;
      center[2] += corner[i][2] / 4;
    }
    spike(center, f.n, 0.24 + rnd() * 0.12, 0.35 + rnd() * 0.25);
  }

  const buffer = (arr) => {
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(arr), gl.STATIC_DRAW);
    return b;
  };
  rockBuffer = buffer(rock);
  rockNormalBuffer = makeNormalBuffer(normalsFromTriangles(rock));
  rockVertexCount = rock.length / 3;
  crystalBuffer = buffer(crystals);
  crystalNormalBuffer = makeNormalBuffer(normalsFromTriangles(crystals));
  crystalVertexCount = crystals.length / 3;
}

// Line-of-sight: an entity is hidden if an asteroid (wall rock) blocks
// the segment from the camera to it (D25 fog-of-war, no seeing through rock).
function occludedByRock(ex, ey, ez) {
  for (const d of debris) {
    if (d.harvested || d.type !== "asteroid") continue;
    const r = (d.size || 1) * 0.55 + 0.6; // rock radius + margin
    const cx = d.pos[0], cy = d.pos[1], cz = d.pos[2];
    const dx = ex - camEye[0], dy = ey - camEye[1], dz = ez - camEye[2];
    const fx = camEye[0] - cx, fy = camEye[1] - cy, fz = camEye[2] - cz;
    const a = dx*dx + dy*dy + dz*dz;
    if (a < 1e-6) continue;
    const b = 2 * (fx*dx + fy*dy + fz*dz);
    const c = fx*fx + fy*fy + fz*fz - r*r;
    const disc = b*b - 4*a*c;
    if (disc < 0) continue;
    const sq = Math.sqrt(disc);
    const t1 = (-b - sq) / (2*a);
    const t2 = (-b + sq) / (2*a);
    // Hit only if the crossing is strictly between camera and target.
    if ((t1 > 0.02 && t1 < 0.98) || (t2 > 0.02 && t2 < 0.98)) return true;
  }
  return false;
}

function drawDebris() {
  if (!rockBuffer || !crystalBuffer) return;
  const loc = gl.getAttribLocation(program, "a_position");
  const uModel = gl.getUniformLocation(program, "u_model");
  const uColor = gl.getUniformLocation(program, "u_color");
  gl.uniform1f(emissiveLoc, 0);

  // The colour says what a chunk is; its size comes from the world data, so
  // a dense metal ore is drawn as the heavy boulder it is (D38).
  const styles = {
    asteroid:    { crystal: [0.42, 0.30, 0.19, 1], rock: [0.19, 0.18, 0.17, 1] },
    ice_chunk:   { crystal: [0.25, 0.85, 1.00, 0.92], rock: [0.14, 0.21, 0.26, 1] },
    scrap_metal: { crystal: [0.78, 0.83, 0.88, 1], rock: [0.20, 0.22, 0.24, 1] },
  };
  for (const d of debris) {
    if (d.harvested) continue;
    if (d.type !== "asteroid" && occludedByRock(d.pos[0], d.pos[1], d.pos[2])) continue;
    const [x, y, z] = d.pos;
    const angle = (parseInt(String(d.id).replace(/\D/g, ""), 10) || 0) * 0.7;
    const s = styles[d.type] || styles.asteroid;
    // The same scale the collision radius uses, so what you see is what you
    // hit.
    const scale = (d.size || 1) * 0.55;
    gl.uniformMatrix4fv(uModel, false, mat4.modelRotZ(x, y, z, scale, angle));

    gl.bindBuffer(gl.ARRAY_BUFFER, rockBuffer);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
    bindNormals(rockNormalBuffer);
    gl.uniform4fv(uColor, s.rock);
    gl.drawArrays(gl.TRIANGLES, 0, rockVertexCount);

    gl.bindBuffer(gl.ARRAY_BUFFER, crystalBuffer);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
    bindNormals(crystalNormalBuffer);
    gl.uniform4fv(uColor, s.crystal);
    gl.drawArrays(gl.TRIANGLES, 0, crystalVertexCount);
  }
}

// ── mini-map ────────────────────────────────────────────────────────────
function initMiniMap() {
  miniCanvas = document.querySelector("#minimap canvas");
}

function drawMiniMap() {
  // The cockpit carries its own tactical display; the 2-D HUD would overlap
  // the new vitals panel in first person.
  if (!miniCanvas || camZoom <= 2.5) return;
  const ctx = miniCanvas.getContext("2d");
  if (!ctx) return;

  const size = 150;
  miniCanvas.width = size;
  miniCanvas.height = size;
  ctx.fillStyle = "rgba(0,0,0,0.7)";
  ctx.fillRect(0, 0, size, size);

  // Draw cube outline: 64×64 mapped to 150px
  const scale = size / 64;
  ctx.strokeStyle = "rgba(50,50,50,0.8)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  // outer square
  ctx.rect(10, 10, 64 * scale, 64 * scale);
  // inner fogged area: draw semi-over black
  ctx.fillStyle = "rgba(0,0,0,0.5)";
  // small viper silhouette in the mini-map
  const px = (player.pos[0] + 0.5) * scale + 10;
  // +z (north) points up on the mini-map, matching the 3D view.
  const py = 10 + (63 - player.pos[2] + 0.5) * scale;
  ctx.fillStyle = "rgba(0,200,0,0.9)";
  ctx.beginPath();
  const shipSize = 6;
  const half = shipSize / 2;
  // delta-wing silhouette with twin tail fins; nose points +z (up on map)
  ctx.moveTo(px, py - half * 1.1);
  ctx.lineTo(px + half * 0.95, py + half * 0.5);
  ctx.lineTo(px + half * 0.35, py + half * 0.3);
  ctx.lineTo(px + half * 0.30, py + half * 0.95);
  ctx.lineTo(px, py + half * 0.55);
  ctx.lineTo(px - half * 0.30, py + half * 0.95);
  ctx.lineTo(px - half * 0.35, py + half * 0.3);
  ctx.lineTo(px - half * 0.95, py + half * 0.5);
  ctx.closePath();
  ctx.fill();

  // Draw NPCs as red dots
  for (let npc of npcs) {
    const nx = (npc.pos[0] + 0.5) * scale + 10;
    const ny = 10 + (63 - npc.pos[2] + 0.5) * scale;
    ctx.fillStyle = "rgba(255,0,0,0.8)";
    ctx.beginPath();
    ctx.arc(nx, ny, 2, 0, Math.PI * 2);
    ctx.fill();
  }

  // Teammates in amber with bearing lines for coordination.
  for (const mate of Object.values(otherPilots)) {
    if (!mate.pos) continue;
    const mx = 10 + mate.pos[0] * scale;
    const my = 10 + (63 - mate.pos[2]) * scale;
    ctx.strokeStyle = "rgba(127,196,255,0.5)";
    ctx.beginPath();
    ctx.moveTo(px, py); ctx.lineTo(mx, my); ctx.stroke();
    ctx.fillStyle = "rgba(127,196,255,0.95)";
    ctx.beginPath();
    ctx.arc(mx, my, 2.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // Draw harvestable debris as small type-coloured dots
  const debrisColors = {
    asteroid: "rgba(180,150,120,0.9)",
    ice_chunk: "rgba(120,210,255,0.9)",
    scrap_metal: "rgba(200,205,210,0.9)",
  };
  for (const d of debris) {
    if (d.harvested) continue;
    const dx = 10 + d.pos[0] * scale;
    const dy = 10 + (63 - d.pos[2]) * scale;
    ctx.fillStyle = debrisColors[d.type] || "rgba(200,200,200,0.9)";
    ctx.fillRect(dx - 1.5, dy - 1.5, 3, 3);
  }

  // Salvage waiting to be grabbed.
  for (const l of worldLoot) {
    if (!l.pos) continue;
    const lx = 10 + l.pos[0] * scale;
    const ly = 10 + (63 - l.pos[2]) * scale;
    ctx.fillStyle = "rgba(94,198,210,0.95)";
    ctx.beginPath();
    ctx.moveTo(lx, ly - 3.5);
    ctx.lineTo(lx + 3.5, ly);
    ctx.lineTo(lx, ly + 3.5);
    ctx.lineTo(lx - 3.5, ly);
    ctx.closePath();
    ctx.fill();
  }

  // Draw portal at its authoritative (x,z) position on the mini-map
  const portalX = 10 + portal.pos[0] * scale;
  const portalY = 10 + (63 - portal.pos[2]) * scale;
  ctx.fillStyle = "rgba(255,255,0,0.9)";
  ctx.beginPath();
  ctx.arc(portalX, portalY, 4, 0, Math.PI * 2);
  ctx.fill();

  // Draw fog: a dark veil over what the pilot has not charted. The scan
  // (C) is what reveals it; the veil only dims the whole map.
  ctx.fillStyle = "rgba(0,0,0,0.3)";
  ctx.fillRect(0, 0, size, size);

  // Ship vitals + onboard store readouts (D25)
  ctx.fillStyle = "rgba(0,0,0,0.72)";
  ctx.fillRect(8, 115, 134, 34);
  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "left";
  const r = (v) => Math.round(v || 0);
  ctx.fillStyle = "#ff9a9a";
  ctx.fillText(`HP ${r(vitals.hp)}/${r(vitals.max_hp)}  SH ${r(vitals.shields)}/${r(vitals.max_shields)}`, 12, 126);
  ctx.fillStyle = "#ffe08a";
  ctx.fillText(`EN ${r(vitals.energy)}/${r(vitals.max_energy)}`, 12, 136);
  ctx.fillStyle = "#7fd0ff";
  ctx.fillText(`CH4 ${r(vitals.methane)}/${r(vitals.max_methane)}`, 12, 146);
  ctx.fillStyle = "#d0d4d8";
  ctx.fillText(`MET ${r(vitals.metal)}/${r(vitals.max_metal)}`, 78, 146);
}

// Hit marker: a brief cross at the gunsight. Combat feedback belongs on
// the HUD, not in the chat transcript (which would flood during a fight).
let hitMarkerUntil = 0;
// Throttle stamps start far in the past so the first message is not
// swallowed by the throttle window.
let lastNoTargetHint = -1e9;
let lastPvpWarning = -1e9;
let lastBounceHint = -1e9;
function showHitMarker(kind) {
  const el = document.querySelector("#hit-marker");
  if (!el) return;
  el.classList.remove("show", "hurt", "dead");
  if (kind) el.classList.add(kind);
  el.classList.add("show");
  hitMarkerUntil = nowMs() + 260;
}

// Downed state: the server owns the respawn timer, the client just counts
// it down for the overlay.
let downed = false;
let downedSince = 0;
let respawnMs = 5000;
function updateDeathOverlay() {
  const el = document.querySelector("#death-overlay");
  if (!el) return;
  if (downed) {
    const left = Math.max(0, Math.ceil((respawnMs - (nowMs() - downedSince)) / 1000));
    const count = document.getElementById("death-count");
    if (count) count.textContent = String(left);
    if (!el.classList.contains("show")) el.classList.add("show");
  } else if (el.classList.contains("show")) {
    el.classList.remove("show");
  }
}

// The Level 1 objective: kill the NPC guarding the wormhole, then take its
// salvage. The banner sits above the squad panel so the eye finds it.
let objectiveState = "";
// The wormhole opens once nothing guards it any more.
let portalOpen = false;
function updateObjective() {
  const el = document.getElementById("hud-objective");
  if (!el) return;
  let text;
  let done = false;
  if (objectiveState === "complete") {
    text = "Level complete — you slipped through to the exit";
    done = true;
  } else if (objectiveState === "salvage" || objectiveState === "done") {
    if (worldLoot.length > 0) {
      text = "Take the salvage — fly to the wreck and press G";
    } else if (portalOpen) {
      text = "The wormhole is open — press E to leave";
    } else {
      text = "Salvage secured — the wormhole is quiet";
      done = true;
    }
  } else if (npcs.length > 0) {
    text = "Destroy the NPC guarding the wormhole";
  } else {
    text = objectiveState === "done"
      ? "Salvage secured — the wormhole is quiet"
      : "Scanning for contacts";
    done = objectiveState === "done";
  }
  if (text !== objectiveText) {
    objectiveText = text;
    el.textContent = text;
  }
  el.classList.toggle("done", done);
}
let objectiveText = "";

// ── waypoint: the objective made findable ──────────────────────────────
// Where the pilot should be heading, and how far away it is.
function objectiveTarget() {
  if (objectiveState === "salvage" || objectiveState === "done") {
    if (worldLoot.length) return { pos: worldLoot[0].pos, label: "salvage" };
    // Nothing left to collect: guide the pilot to the spawn-facing side
    // of the wormhole, not its centre — overshooting puts you on the wrong
    // side and the engine rightly refuses entry.
    if (portalOpen && portal.pos) {
      const p = portal.pos;
      const front = portal.front || "-z";
      const approach = front === "-z" ? [p[0], p[1], p[2] - 4]
                     : front === "+z" ? [p[0], p[1], p[2] + 4]
                     : front === "+x" ? [p[0] - 4, p[1], p[2]]
                                      : [p[0] + 4, p[1], p[2]];
      return { pos: approach, label: "wormhole" };
    }
    return null;
  }
  if (npcs.length) {
    // The closest living NPC.
    let best = null;
    let bestDist = Infinity;
    for (const n of npcs) {
      if (!n.pos) continue;
      const d = Math.hypot(n.pos[0] - player.pos[0], n.pos[1] - player.pos[1],
                           n.pos[2] - player.pos[2]);
      if (d < bestDist) { bestDist = d; best = n; }
    }
    if (best) return { pos: best.pos, label: "npc" };
  }
  return null;
}

// Project a world point into normalised device space; null when behind us.
function projectToNdc(p) {
  const vp = mat4.multiply(projMatrix, viewMatrix);
  const x = vp[0] * p[0] + vp[4] * p[1] + vp[8] * p[2] + vp[12];
  const y = vp[1] * p[0] + vp[5] * p[1] + vp[9] * p[2] + vp[13];
  const w = vp[3] * p[0] + vp[7] * p[1] + vp[11] * p[2] + vp[15];
  if (w <= 0.0001) return null;
  return { x: x / w, y: y / w };
}

// Where a world point sits on screen, clamped to the edge when it is off it.
function screenPoint(p) {
  const w = canvas.clientWidth || canvas.width;
  const h = canvas.clientHeight || canvas.height;
  const ndc = projectToNdc(p);
  const margin = 44;
  if (ndc && Math.abs(ndc.x) <= 1 && Math.abs(ndc.y) <= 1) {
    return {
      x: (ndc.x * 0.5 + 0.5) * w,
      y: (1 - (ndc.y * 0.5 + 0.5)) * h,
      angle: 0, onScreen: true,
    };
  }
  // Off screen or behind the camera: point the way.
  let dx = 1;
  let dy = 0;
  if (ndc) {
    dx = ndc.x;
    dy = -ndc.y;
  } else {
    const f = viewForward();
    const rxv = p[0] - player.pos[0];
    const rzv = p[2] - player.pos[2];
    dx = rxv * f[2] - rzv * f[0];
    dy = -(rxv * f[0] + rzv * f[2]);
    if (dx === 0 && dy === 0) dx = 1;
  }
  const len = Math.hypot(dx, dy) || 1;
  dx /= len; dy /= len;
  const scale = Math.min((w / 2 - margin) / Math.max(1e-3, Math.abs(dx)),
                         (h / 2 - margin) / Math.max(1e-3, Math.abs(dy)));
  return {
    x: w / 2 + dx * scale,
    y: h / 2 + dy * scale,
    angle: Math.atan2(dy, dx) * 180 / Math.PI,
    onScreen: false,
  };
}

function updateWaypoint() {
  const el = document.querySelector("#waypoint");
  if (!el) return;
  const target = objectiveTarget();
  if (!target) {
    el.classList.remove("show");
    return;
  }
  const pt = screenPoint(target.pos);
  const dist = Math.hypot(target.pos[0] - player.pos[0], target.pos[1] - player.pos[1],
                           target.pos[2] - player.pos[2]);
  el.style.left = `${pt.x}px`;
  el.style.top = `${pt.y}px`;
  const chev = el.querySelector(".chev");
  if (chev) chev.style.transform = `rotate(${pt.angle}deg)`;
  const range = document.getElementById("waypoint-range");
  if (range) range.textContent = `${Math.round(dist)}u`;
  el.classList.add("show");
}

// Damage direction: a red chevron pointing at whoever just hit us.
let damageArrowUntil = 0;
function showDamageArrow(fromPos) {
  const el = document.querySelector("#damage-arrow");
  if (!el || !fromPos) return;
  const pt = screenPoint(fromPos);
  el.style.left = `${pt.x}px`;
  el.style.top = `${pt.y}px`;
  const chev = el.querySelector(".chev");
  if (chev) chev.style.transform = `rotate(${pt.angle}deg)`;
  el.classList.add("show");
  damageArrowUntil = nowMs() + 900;
}

// Ship status indicator next to the tactical map: rear thrusters light on
// forward thrust, front reverse thrusters on reverse, guns on while firing.
function updateShipStatus() {
  const rear = document.getElementById("light-rear");
  const front = document.getElementById("light-front");
  const gun = document.getElementById("light-gun");
  if (rear) rear.className = "light" + (thrustHeld > 0 || thrust > 0.05 ? " on-rear" : "");
  if (front) front.className = "light" + (thrustHeld < 0 ? " on-front" : "");
  if (gun) gun.className = "light" + (nowMs() < fireUntil ? " on-gun" : "");
}

// ── target reticle + lead pip (D57) ─────────────────────────────────────
// HUD-only aiming aid: brackets on the pilot or guard we would shoot, and
// a pip where that target will be after `aim_lead_ms` of its measured
// drift. The server still judges every shot (D2) — this only aims the eye.
let projectileSpeed = 55; // u/s — how fast a bolt flies (D59)
function updateReticle() {
  const ret = document.querySelector("#reticle");
  const pip = document.querySelector("#lead-pip");
  const label = document.querySelector("#reticle-label");
  if (!ret || !pip) return;
  const target = nearestFireTarget();
  const isMob = target && npcs.some(n => n.id === target);
  const mate = (!isMob && target) ? otherPilots[target] : null;
  const pos = isMob
    ? ((npcs.find(n => n.id === target) || {}).pos)
    : (mate && mate.pos);
  if (!target || !pos) {
    ret.classList.remove("show");
    pip.classList.remove("show");
    if (label) label.classList.remove("show");
    return;
  }
  const pt = screenPoint(pos);
  if (!pt.onScreen) {
    // Off screen: the brackets would just be noise at the edge.
    ret.classList.remove("show");
    pip.classList.remove("show");
    if (label) label.classList.remove("show");
    return;
  }
  ret.style.left = `${pt.x}px`;
  ret.style.top = `${pt.y}px`;
  ret.classList.add("show");
  // What we are aiming at, and how much of it is left (D58).
  if (label) {
    let name = target;
    let shields = null;
    let hp = null;
    if (isMob) {
      const npc = npcs.find(n => n.id === target) || {};
      name = npc.name || target;
      hp = npc.hp;
    } else {
      shields = mate.shields;
      hp = mate.hp;
    }
    const parts = [];
    if (shields !== undefined && shields !== null) parts.push(`SH ${Math.round(shields)}`);
    if (hp !== undefined && hp !== null) parts.push(`HP ${Math.round(hp)}`);
    label.innerHTML = parts.length
      ? `${name} · <b>${parts.join(" · ")}</b>`
      : name;
    label.style.left = `${pt.x}px`;
    label.style.top = `${pt.y + 24}px`;
    label.classList.add("show");
  }
  if (mate && mate.vel) {
    const dist = Math.hypot(pos[0] - player.pos[0],
                            pos[1] - player.pos[1],
                            pos[2] - player.pos[2]);
    // Lead for the real flight time: dist / speed (D59).
    const leadTime = dist / Math.max(1, projectileSpeed);
    const lead = [
      pos[0] + mate.vel[0] * leadTime,
      pos[1] + mate.vel[1] * leadTime,
      pos[2] + mate.vel[2] * leadTime,
    ];
    const lp = screenPoint(lead);
    if (lp.onScreen) {
      pip.style.left = `${lp.x}px`;
      pip.style.top = `${lp.y}px`;
      pip.classList.add("show");
    } else {
      pip.classList.remove("show");
    }
  } else {
    pip.classList.remove("show");
  }
}

// ── kill feedback (D57) ─────────────────────────────────────────────────
// A kill is the loudest moment in the fight: banner, gold wash, shake.
let killBannerUntil = 0;
function showKillBanner(text) {
  const el = document.querySelector("#kill-banner");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("show");
  void el.offsetWidth; // restart the animation on a rapid second kill
  el.classList.add("show");
  killBannerUntil = nowMs() + 1600;
}
function showKillFlash() {
  const el = document.querySelector("#kill-flash");
  if (!el) return;
  el.classList.remove("show");
  void el.offsetWidth;
  el.classList.add("show");
}
function updateKillFeedback() {
  const el = document.querySelector("#kill-banner");
  if (el && killBannerUntil && nowMs() >= killBannerUntil) {
    el.classList.remove("show");
    killBannerUntil = 0;
  }
}


// Speed, measured from the authoritative position frames the client
// receives — the same displacement the server uses for collision damage.
let lastPosSample = null;
let lastPosAt = 0;
let speedEstimate = 0;
function measuredSpeed() {
  const now = nowMs();
  const last = lastPosSample;
  if (!last) {
    lastPosSample = player.pos.slice();
    lastPosAt = now;
    return 0;
  }
  const dt = (now - lastPosAt) / 1000;
  if (dt > 0.05) {
    const d = Math.hypot(player.pos[0] - last[0], player.pos[1] - last[1],
                          player.pos[2] - last[2]);
    // Smooth it so the needle does not flicker.
    speedEstimate = speedEstimate * 0.6 + (d / dt) * 0.4;
    lastPosSample = player.pos.slice();
    lastPosAt = now;
  }
  return speedEstimate;
}

// ── cockpit HUD and fullscreen ─────────────────────────────────────────
// Fixed zones: squad top-centre, own ship bottom-left, tactical map
// bottom-right. The eye always finds the same information in the same place.
let hudEl = null;
let helpEl = null;
let helpShownOnce = false;
let tacticalCanvas = null;

function toggleFullscreen() {
  const el = document.documentElement;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (exit) exit.call(document);
  } else {
    const enter = el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen;
    if (enter) enter.call(el);
  }
}

function updateCockpitHUD() {
  if (!hudEl) return;
  const firstPerson = camZoom <= 2.5;
  hudEl.classList.toggle("active", firstPerson);
  doEngageBtn();
  hudEl.classList.toggle("hurt", nowMs() < damageFlashUntil);
  // The join panel steps aside once the pilot is in the cockpit.
  document.body.classList.toggle("in-flight", firstPerson);
  const marker = document.querySelector("#hit-marker");
  if (marker && nowMs() >= hitMarkerUntil) marker.classList.remove("show");
  updateDeathOverlay();
  updateObjective();
  updateWaypoint();
  const dmgArrow = document.querySelector("#damage-arrow");
  if (dmgArrow && nowMs() >= damageArrowUntil) dmgArrow.classList.remove("show");
  const minimap = document.querySelector("#minimap");
  if (minimap) minimap.style.display = firstPerson ? "none" : "block";
  updateReticle();
  updateShipStatus();
  updateKillFeedback();
  if (!firstPerson) return;

  const set = (id, value, barId, cap) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(Math.round(value || 0));
    const bar = document.getElementById(barId);
    if (bar) bar.style.width = `${cap > 0 ? Math.max(0, Math.min(100, value / cap * 100)) : 0}%`;
  };
  set("v-hp", vitals.hp, "b-hp", vitals.max_hp);
  set("v-sh", vitals.shields, "b-sh", vitals.max_shields);
  set("v-en", vitals.energy, "b-en", vitals.max_energy);
  set("v-ch4", vitals.methane, "b-ch4", vitals.max_methane);
  set("v-met", vitals.metal, "b-met", vitals.max_metal);
  // Rank and progress toward the next level.
  const level = Math.max(1, vitals.level || 1);
  const base = vitals.xp_base || 0;
  const next = vitals.xp_next || 100;
  const levelEl = document.getElementById("v-level");
  if (levelEl) levelEl.textContent = String(level);
  const xpBar = document.getElementById("b-xp");
  if (xpBar) {
    const span = Math.max(1, next - base);
    const done = Math.max(0, Math.min(span, (vitals.xp || 0) - base));
    xpBar.style.width = `${Math.max(0, Math.min(100, done / span * 100))}%`;
  }
  // Energy cells in the hold.
  const cells = (vitals.artifacts || []).filter(a => a === "energy_cell").length;
  const cellsEl = document.getElementById("v-cells");
  if (cellsEl) cellsEl.textContent = String(cells);
  // Speed, measured from the authoritative frames: how fast the ship is
  // really travelling, which is what decides how hard a rock will hit.
  const speedEl = document.getElementById("v-speed");
  if (speedEl) speedEl.textContent = String(Math.round(measuredSpeed()));

  // Drop pilots we have not heard from for 30 seconds.
  for (const name of Object.keys(otherPilots)) {
    if (nowMs() - (otherPilots[name].seen || 0) > 30000) delete otherPilots[name];
  }

  const rows = document.getElementById("hud-squad-rows");
  if (rows) {
    rows.textContent = "";
    const line = (label, value, color) => {
      const div = document.createElement("div");
      div.className = "squad-row";
      const l = document.createElement("span");
      l.textContent = label;
      l.style.color = color || "#eaffff";
      const v = document.createElement("span");
      v.textContent = value;
      div.append(l, v);
      rows.appendChild(div);
    };
    line("YOU", `${Math.round(vitals.hp)} HP · ${Math.round(vitals.shields)} SH · `
      + `${Math.round(vitals.energy)} EN`, "#8be9a8");
    for (const [name, mate] of Object.entries(otherPilots)) {
      if (!mate.pos) continue;
      const dist = Math.hypot(mate.pos[0] - player.pos[0],
                              mate.pos[1] - player.pos[1],
                              mate.pos[2] - player.pos[2]);
      const vitalsText = mate.hp === undefined ? "vitals unknown"
        : `${Math.round(mate.hp)} HP · ${Math.round(mate.shields || 0)} SH`;
      const tag = name === affiliate ? " ⭐" : "";
      line(name + tag, `${vitalsText} · ${dist.toFixed(0)}u`,
           name === affiliate ? "#9fd7ff" : "#7fc4ff");
    }
  }
  drawTacticalMap();
}

function drawTacticalMap() {
  if (!tacticalCanvas) return;
  const ctx = tacticalCanvas.getContext("2d");
  if (!ctx) return;
  const S = tacticalCanvas.width;
  const cx = S / 2, cy = S / 2, R = S / 2 - 8;
  ctx.clearRect(0, 0, S, S);
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.clip();
  ctx.fillStyle = "#03141b";
  ctx.fillRect(0, 0, S, S);
  // Concentric rings and spokes, like a real radar scope.
  ctx.strokeStyle = "rgba(94,198,210,0.22)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    ctx.beginPath(); ctx.arc(cx, cy, R * i / 4, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(cx, 0); ctx.lineTo(cx, S);
  ctx.moveTo(0, cy); ctx.lineTo(S, cy);
  ctx.stroke();
  // A slow sweep, so the scope reads as a live instrument.
  const sweep = (nowMs() / 1000 * 0.9) % (Math.PI * 2);
  const gx = cx + Math.cos(sweep) * R, gy = cy + Math.sin(sweep) * R;
  const grad = ctx.createLinearGradient(cx, cy, gx, gy);
  grad.addColorStop(0, "rgba(94,198,210,0.4)");
  grad.addColorStop(1, "rgba(94,198,210,0)");
  ctx.strokeStyle = grad;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(gx, gy); ctx.stroke();
  const pad = 12;
  const span = S - pad * 2;
  const px = (wx) => pad + (wx / 63) * span;
  const py = (wz) => S - pad - (wz / 63) * span; // +z points up

  // Bearing lines link the flight leader to each teammate for coordination.
  for (const mate of Object.values(otherPilots)) {
    if (!mate.pos) continue;
    ctx.strokeStyle = "rgba(127,196,255,0.5)";
    ctx.beginPath();
    ctx.moveTo(px(player.pos[0]), py(player.pos[2]));
    ctx.lineTo(px(mate.pos[0]), py(mate.pos[2]));
    ctx.stroke();
  }
  const marker = (wx, wz, color, radius) => {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px(wx), py(wz), radius, 0, Math.PI * 2);
    ctx.fill();
  };
  for (const d of debris) {
    if (d.harvested) continue;
    const mass = d.mass || 0;
    const heavy = mass >= 20;
    const c = d.material === "rock" ? (heavy ? "#c98a4b" : "#8a6a45")
            : d.material === "methane" ? "#48ccff" : "#e8eef2";
    // Heavier chunks are drawn bigger: the mass is what you fly into.
    marker(d.pos[0], d.pos[2], c, heavy ? 5 : 3);
  }
  // Scan-discovered debris get a bright marker that fades with the TTL.
  for (const d of debris) {
    if (d.harvested) continue;
    if (!(scanBlips[d.id] > nowMs())) continue;
    marker(d.pos[0], d.pos[2], "#7dff9b", 4);
  }
  // Contacts shared by a wingman: hollow markers in their colour.
  for (const d of debris) {
    if (d.harvested) continue;
    const shared = sharedBlips[d.id];
    if (!shared || shared.until < nowMs()) continue;
    ctx.strokeStyle = "#9fd7ff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(px(d.pos[0]), py(d.pos[2]), 5, 0, Math.PI * 2);
    ctx.stroke();
  }
  for (const npc of npcs) marker(npc.pos[0], npc.pos[2], "#ff5555", 4);
  // Salvage waiting to be grabbed.
  for (const l of worldLoot) {
    if (!l.pos) continue;
    ctx.fillStyle = "#5ec6d2";
    ctx.beginPath();
    ctx.moveTo(px(l.pos[0]), py(l.pos[2]) - 5);
    ctx.lineTo(px(l.pos[0]) + 5, py(l.pos[2]));
    ctx.lineTo(px(l.pos[0]), py(l.pos[2]) + 5);
    ctx.lineTo(px(l.pos[0]) - 5, py(l.pos[2]));
    ctx.closePath();
    ctx.fill();
  }
  marker(spawnPortal.pos[0], spawnPortal.pos[2], "#ff8a3c", 6);
  marker(portal.pos[0], portal.pos[2], "#ffe34d", 7);
  for (const mate of Object.values(otherPilots)) {
    if (mate.pos) marker(mate.pos[0], mate.pos[2], "#7fc4ff", 6);
  }
  marker(player.pos[0], player.pos[2], "#7dff9b", 6);
  ctx.restore(); // lift the circular clip
  // Instrument bezel.
  ctx.strokeStyle = "rgba(94,198,210,0.7)";
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
}

// ── input handling ─────────────────────────────────────────────────────

function handleKeyDown(e) {
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  const key = e.key.toLowerCase();
  // Simple forward/back/left/right based on current look direction
  // For Level 1 proto, we'll just update pos based on absolute directions
  // (the server will validate/rehash; client just sends intent)
  if (key === "v") { toggleCamera(); return; }
  if (key === "h" || key === "?") { toggleHelp(); return; }
  if (key === "m") {
    if (hudEl) hudEl.classList.toggle("min");
    return;
  }
  if (key === "f2" || (e.altKey && key === "enter")) { toggleFullscreen(); return; }

  // Arrow keys rotate the view (mouse-free alternative to pointer lock).
  if (key === "arrowleft")  { viewYaw -= 0.12; e.preventDefault(); return; }
  if (key === "arrowright") { viewYaw += 0.12; e.preventDefault(); return; }
  if (key === "arrowup")    { viewPitch = Math.min(PITCH_LIMIT, viewPitch + 0.10); e.preventDefault(); return; }
  if (key === "arrowdown")  { viewPitch = Math.max(-PITCH_LIMIT, viewPitch - 0.10); e.preventDefault(); return; }

  // T affiliates with a squadmate (or clears the affiliation).
  if (key === "t" && ws && ws.readyState === WebSocket.OPEN) {
    const mate = nearestAffiliate();
    ws.send(JSON.stringify({
      t: "affiliate", name: player.name,
      target: mate && mate !== affiliate ? mate : null,
    }));
  }

  // U uses a carried energy cell to repair the shields.
  if (key === "u" && ws && ws.readyState === WebSocket.OPEN) {
    const cell = (vitals.artifacts || [])[0];
    if (!cell) {
      logChat("No energy cell in the hold — run over salvage to grab it.");
      return;
    }
    ws.send(JSON.stringify({ t: "use", item: cell, name: player.name }));
  }

  // Y shares the scan chart with the wingman we fly with.
  if (key === "y" && ws && ws.readyState === WebSocket.OPEN) {
    const mate = affiliate || nearestAffiliate();
    if (!mate) {
      logChat("No wingman to share the chart with.");
      return;
    }
    ws.send(JSON.stringify({ t: "fog_share", name: player.name, target: mate }));
  }

  // R repairs the nearest hurt wingman in range: energy buys their shields.
  if (key === "r" && ws && ws.readyState === WebSocket.OPEN) {
    const mate = nearestHurtMate();
    if (!mate) {
      logChat("No hurt wingman in range.");
      return;
    }
    ws.send(JSON.stringify({
      t: "repair", name: player.name, target: mate.name, amount: 10,
    }));
  }

  // W/S thrust along the current view vector; pitch down and hold W to dive
  // toward the spawn exit. A/D and Space/Shift remain absolute-axis moves.
  if (key === "w") { thrustHeld = 1; startThrust(); }
  else if (key === "s") { thrustHeld = -1; startThrust(); }
  else if (key === "e" && ws && ws.readyState === WebSocket.OPEN) {
    // Burst away from the nearest threat; the server owns the result.
    sfxEvade();
    ws.send(JSON.stringify({ t: "evade", name: player.name }));
  }
  else if (key === "f" && ws && ws.readyState === WebSocket.OPEN && nearDebris) {
    // Intent only — the server checks distance and marks the chunk harvested.
    ws.send(JSON.stringify({ t: "harvest", id: nearDebris.id, name: player.name }));
  }
}

// Visual feedback for a movement step — never authoritative (D2). Called
// both for local key presses and, crucially, for every authoritative `pos`
// broadcast, so the ship faces the way it actually travels even when the
// intent came from elsewhere (a scripted demo, gravity drift, another input).
function driveVisual(dirs, setHeading = false) {
  for (const dir of dirs) {
    if (DIR_YAW[dir] !== undefined) {
      let delta = DIR_YAW[dir] - shipYaw;
      while (delta > Math.PI) delta -= 2 * Math.PI;
      while (delta < -Math.PI) delta += 2 * Math.PI;
      // Only local key commands steer the view. Authoritative pos broadcasts
      // must not fight the mouse, so they pulse feedback without re-aiming.
      if (setHeading) {
        viewYaw = DIR_YAW[dir];
        viewPitch = 0;
      }
      if (Math.abs(delta) > 0.5) {
        turnBiasTarget = delta > 0 ? -1 : 1;
        biasUntil = nowMs() + 500;
      }
    }
    if (dir === "up" || dir === "down") {
      verticalTarget = dir === "up" ? 1 : -1;
      vertUntil = nowMs() + 180;
    }
    thrustUntil = nowMs() + 180;
  }
}

// Hold-to-thrust: send a 3-D intent along the view vector at tick rate. The
// server clamps the resulting position, so this is still intent only (D2).
let thrustHeld = 0;
let thrustTimer = null;
let pointerLocked = false;
// Damage vignette: armed while the local pilot's HP/shields drop between
// vitals frames, so the HUD flashes red instead of silently draining.
let lastVitals = null;
let damageFlashUntil = 0;
// Camera shake magnitude, armed while the local pilot takes damage.
let shakeUntil = 0;
let shakeMag = 0;
function startThrust() {
  if (thrustTimer) return;
  thrustTimer = setInterval(() => {
    if (!thrustHeld || !ws || ws.readyState !== WebSocket.OPEN) return;
    const f = viewForward();
    const k = thrustHeld;
    ws.send(JSON.stringify({
      t: "move", name: player.name,
      vec: [f[0] * k, f[1] * k, f[2] * k],
    }));
    thrustUntil = nowMs() + 180;
    // Small, frequent authoritative steps read as continuous flight.
  }, 50);
}

function stopThrust() {
  thrustHeld = 0;
  if (thrustTimer) { clearInterval(thrustTimer); thrustTimer = null; }
}

function engine_tick_move(dir) {
  // Send the intent; the server owns the authoritative position (D2).
  driveVisual([dir], true);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ t: "move", dir, name: player.name }));
  }
}

// Nearest known squadmate, for the affiliation key.
function nearestAffiliate() {
  let best = null;
  let bestDist = Infinity;
  for (const [name, mate] of Object.entries(otherPilots)) {
    if (!mate.pos) continue;
    const dist = Math.hypot(mate.pos[0] - player.pos[0],
                            mate.pos[1] - player.pos[1],
                            mate.pos[2] - player.pos[2]);
    if (dist < bestDist) { bestDist = dist; best = name; }
  }
  return best;
}

// Nearest wingman with damaged shields, close enough to repair.
function nearestHurtMate() {
  let best = null;
  let bestDist = 5;
  for (const [name, mate] of Object.entries(otherPilots)) {
    if (!mate.pos || mate.shields === undefined) continue;
    if (mate.shields >= (mate.max_shields || 100)) continue;
    const dist = Math.hypot(mate.pos[0] - player.pos[0],
                            mate.pos[1] - player.pos[1],
                            mate.pos[2] - player.pos[2]);
    if (dist < bestDist) { bestDist = dist; best = { name, shields: mate.shields }; }
  }
  return best;
}

// Closest salvage within reach, for the G key. The radius is slightly
// tighter than the server's nearby_radius so the server always accepts.
function nearestLoot() {
  let best = null;
  let bestDist = 4.5;
  for (const l of worldLoot) {
    if (!l.pos) continue;
    const dist = Math.hypot(l.pos[0] - player.pos[0],
                            l.pos[1] - player.pos[1],
                            l.pos[2] - player.pos[2]);
    if (dist < bestDist) { bestDist = dist; best = l; }
  }
  return best;
}

// Closest harvestable chunk within reach, for the F key and the hint.
let nearDebris = null;
let lastHarvestHint = null;
let lastAutoTake = 0;
function checkDebrisHint() {
  let best = null;
  let bestDist = 5;
  for (const d of debris) {
    if (d.harvested) continue;
    const dist = Math.hypot(player.pos[0] - d.pos[0],
                            player.pos[1] - d.pos[1],
                            player.pos[2] - d.pos[2]);
    if (dist < bestDist) {
      bestDist = dist;
      best = d;
    }
  }
  nearDebris = best;
  if (best && best.id !== lastHarvestHint) {
    lastHarvestHint = best.id;
    logChat(`${best.name} nearby — press F to harvest.`);
  } else if (!best) {
    lastHarvestHint = null;
  }
  // Auto-pickup: running over salvage grabs it (no G key needed).
  const loot = nearestLoot();
  if (loot && ws && ws.readyState === WebSocket.OPEN && nowMs() - lastAutoTake > 600) {
    lastAutoTake = nowMs();
    ws.send(JSON.stringify({ t: "take", item: loot.id, name: player.name }));
  }
}

// One-time hint when the pilot is lined up on the entry side of the wormhole.
let nearPortal = false;
function checkPortalHint() {
  const pp = portal.pos;
  const dx = player.pos[0] - pp[0];
  const dy = player.pos[1] - pp[1];
  const dz = player.pos[2] - pp[2];
  const depth = portal.front === "+z" ? -dz : (portal.front === "+x" ? -dx : dz);
  const lateral = portal.front === "+x" ? dz : dx;
  const inZone = depth <= 0 && depth >= -6 &&
                 Math.abs(lateral) <= portal.aperture + 1 &&
                 Math.abs(dy) <= portal.aperture + 1;
  if (inZone) {
    if (!nearPortal) {
      nearPortal = true;
      logChat("Wormhole ahead — flying in enters it.");
    }
    // Auto-enter: running into the wormhole carries you through (no E key).
    if (ws && ws.readyState === WebSocket.OPEN && nowMs() - (window._lastPortalEnter || 0) > 1500) {
      window._lastPortalEnter = nowMs();
      sfxPortal();
      ws.send(JSON.stringify({ t: "enter_portal", name: player.name }));
    }
  } else {
    nearPortal = false;
  }
}

function handleMouseMove(e) {
  // Pointer lock is the primary path; dragging with the button held also
  // works where pointer lock is unavailable. Hovering alone never re-aims.
  const locked = document.pointerLockElement === canvas;
  if (!locked && e.buttons !== 1) return;
  viewYaw += (e.movementX || 0) * LOOK_SENS;
  viewPitch = Math.max(-PITCH_LIMIT,
    Math.min(PITCH_LIMIT, viewPitch - (e.movementY || 0) * LOOK_SENS));
}

function handleKeyUp(e) {
  const k = e.key.toLowerCase();
  if (k === "w" || k === "s") stopThrust();
}

// Prototype direct fire: pick the nearest valid target (teammate or mob)
// within the server-authoritative range, then let the server judge the shot.
// Mobs come first — they are the obvious threat — and the wingman we fly
// with is never picked by accident. Only what the pilot is looking at can
// be shot: a gun does not fire behind your own head.
//
// D58: a pilot we have not heard from for a moment is a ghost (they left,
// or they were never really there) and is never a target; and the pilot we
// are actually fighting wins over a guard that happens to be in the cone.
const PILOT_STALE_MS = 5000;
function pilotPresent(mate) {
  return !!mate && nowMs() - (mate.seen || 0) <= PILOT_STALE_MS;
}
function nearestFireTarget() {
  let best = null;
  let bestDist = Infinity;
  const f = viewForward();
  const consider = (id, pos) => {
    const dx = pos[0] - player.pos[0];
    const dy = pos[1] - player.pos[1];
    const dz = pos[2] - player.pos[2];
    const dist = Math.hypot(dx, dy, dz);
    if (dist > fireRange) return;
    // A generous cone: roughly 120 degrees around the nose.
    const dot = (dx * f[0] + dy * f[1] + dz * f[2]) / (dist || 1);
    if (dot < -0.5) return;
    if (dist < bestDist) { bestDist = dist; best = id; }
  };
  // Our fight partner wins the cone when they are there (D58).
  if (fightTarget) {
    const mate = otherPilots[fightTarget];
    if (pilotPresent(mate) && mate.pos) {
      const dx = mate.pos[0] - player.pos[0];
      const dy = mate.pos[1] - player.pos[1];
      const dz = mate.pos[2] - player.pos[2];
      const dist = Math.hypot(dx, dy, dz);
      const dot = (dx * f[0] + dy * f[1] + dz * f[2]) / (dist || 1);
      if (dist <= fireRange && dot >= -0.5) return fightTarget;
    }
  }
  for (const npc of npcs) {
    if (npc && npc.pos) consider(npc.id, npc.pos);
  }
  // Debris (rocks, methane, metal) are practice targets: shots register a
  // hit but deal no damage server-side.
  for (const d of debris) {
    if (d && d.pos && !d.harvested) consider(d.id, d.pos);
  }
  if (best) return best;
  for (const [name, mate] of Object.entries(otherPilots)) {
    if (!mate.pos) continue;
    if (name === affiliate) continue;  // never shoot the pilot you fly with
    if (!pilotPresent(mate)) continue; // a ghost is not a target (D58)
    consider(name, mate.pos);
  }
  return best;
}

function doFire() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const target = nearestFireTarget();
  if (!target) {
    // Throttle: a pilot clicking with nothing in range should not flood the
    // transcript (and the marker already shows the shot did not go out).
    const now = nowMs();
    if (now - lastNoTargetHint > 3000) {
      lastNoTargetHint = now;
      logChat("No target in range — fly into the fight.");
      showHitMarker("hurt");
    }
    return;
  }
  ws.send(JSON.stringify({ t: "fire", target, name: player.name }));
  sfxShoot();
  fireUntil = nowMs() + 140;
  // Shooting another pilot is deliberate, not accidental: say so.
  const isMob = npcs.some(n => n.id === target);
  if (!isMob && nowMs() - lastPvpWarning > 5000) {
    lastPvpWarning = nowMs();
    logChat(`Firing on ${target} — a fellow pilot.`);
  }
}

// ── pilot-vs-pilot fighting (D55) ────────────────────────────────────────
// The fight handshake: both pilots agree on the same other pilot; the
// server records the pair and broadcasts "engaged" to both.
function nearestPilot() {
  let best = null, bestDist = Infinity;
  for (const [name, mate] of Object.entries(otherPilots)) {
    if (!mate.pos || name === affiliate) continue;
    if (!pilotPresent(mate)) continue; // a ghost is not a rival (D58)
    const d = Math.hypot(mate.pos[0] - player.pos[0],
                         mate.pos[1] - player.pos[1],
                         mate.pos[2] - player.pos[2]);
    if (d > fireRange) continue;
    if (d < bestDist) { bestDist = d; best = name; }
  }
  return best;
}

function doEngage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const target = nearestPilot();
  if (!target) {
    logChat("No pilot to fight — someone nearby first.");
    return;
  }
  ws.send(JSON.stringify({ t: "engage", name: player.name, target }));
  // The server confirms the pair ("engaged"); do not claim a fight it may
  // have refused (D58).
  sfxShoot();
  logChat(`Challenging ${target}…`);
}

function doDisengage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const target = fightTarget;
  ws.send(JSON.stringify({ t: "disengage", name: player.name, target }));
  fightTarget = null;
  logChat("Back on your own — the fight is off.");
}

function doEngageBtn() {
  const btn = document.querySelector("#fight-btn");
  if (btn) btn.textContent = fightTarget ? "⏹ Disengage" : "⚔ Engage";
  const label = document.querySelector("#fight-status");
  if (label) label.textContent = fightTarget ? `Bombing ${fightTarget}` : "No fight declared";
}

// ── WebSocket connection ────────────────────────────────────────────────

let ws = null;

function logChat(text) {
  const log = document.querySelector("#chat-log");
  const chat = document.querySelector("#chat");
  if (log) {
    // Cap the transcript so long sessions cannot grow the DOM unbounded.
    const items = Array.from(log.children);
    if (items.length > 120) {
      log.innerHTML = items.slice(-60).map((el) => el.outerHTML).join("");
    }
    log.innerHTML += `<div class="msg">${text}</div>`;
  }
  if (chat) chat.scrollTop = chat.scrollHeight;
}

function applyVitals(v) {
  if (!v) return;
  for (const key of ["hp", "shields", "max_hp", "max_shields", "energy",
                     "methane", "metal", "max_energy", "max_methane", "max_metal",
                     "xp", "level", "xp_next", "xp_base", "artifacts"]) {
    if (v[key] !== undefined) vitals[key] = v[key];
  }
  player.hp = vitals.hp;
  player.shields = vitals.shields;
  const prev = lastVitals;
  lastVitals = Object.assign({}, vitals);
  if (prev) {
    if (vitals.hp < prev.hp - 1e-6 || vitals.shields < prev.shields - 1e-6) {
      damageFlashUntil = nowMs() + 450;
      // Camera shake on taking damage; decays over ~180ms.
      shakeUntil = nowMs() + 180;
      shakeMag = Math.max(shakeMag, 1);
    }
    // A wider authoritative hull (max_hp) means we climbed a level.
    if (v && v.max_hp > prev.max_hp + 1e-6) sfxLevelUp();
    // Reaching zero HP is a death; the respawn will re-fire vitals.
    if (vitals.hp <= 0 && prev.hp > 0) {
      sfxDeath();
      downed = true;
      downedSince = nowMs();
      // A downed ship does not burn fuel: stop thrusting.
      stopThrust();
    }
  }
  // Back in the air.
  if (downed && vitals.hp > 0) {
    downed = false;
    logChat("Back in the air.");
  }
}

function connectWS(name, token) {
  player.name = name;
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${wsProto}//${location.host}/ws`);

  ws.onopen = () => {
    console.log("WS connected");
    ws.send(JSON.stringify({ t: "set_name", name, token: token || null }));
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.t === "welcome") {
      player.name = msg.you.name;
      player.pos = msg.you.pos || player.pos;
      renderPos = player.pos.slice();
      applyVitals(msg.you);
      npcs = msg.npcs || [];
      debris = (msg.debris || []).map(d => Object.assign({}, d, { harvested: false }));
      // The squad roster: wingmen who have not moved yet are still listed,
      // with the state the server already knows.
      for (const p of (msg.pilots || [])) {
        if (!p || !p.name || !p.pos) continue;
        const mate = otherPilots[p.name] || (otherPilots[p.name] = {});
        mate.pos = p.pos.slice();
        if (p.hp !== undefined) mate.hp = p.hp;
        if (p.shields !== undefined) mate.shields = p.shields;
        if (p.max_shields !== undefined) mate.max_shields = p.max_shields;
        if (p.energy !== undefined) mate.energy = p.energy;
        mate.seen = nowMs();
      }
      if (msg.portal) {
        portal.pos = msg.portal.pos;
        portal.front = msg.portal.front || "-z";
        portal.aperture = msg.portal.aperture || 4;
      }
      if (msg.spawn_portal) spawnPortal = Object.assign({}, spawnPortal, msg.spawn_portal);
      initCamera();
      fireRange = msg.fire_range || 24;
      projectileSpeed = msg.projectile_speed || 55;
      respawnMs = msg.respawn_ms || respawnMs;
      logChat(`Welcome, ${player.name}.`);
      // A short briefing: the verbs are many, but the pilot only needs the
      // ones that carry the loop.
      logChat("W/S thrust · mouse aims · click fires");
      logChat("Fly over salvage to grab it · fly into the wormhole to enter");
      if (!helpShownOnce) { helpShownOnce = true; showHelpBriefly(9000); }
      logChat("E evades · R repairs a wingman · U uses a cell");
      logChat("T flies with one · Y shares your chart · F harvests");
    }
    // The server broadcasts positions after every move and every gravity
    // drift (authoritative, D2/D25).
    if (msg.t === "pos") {
      if (msg.from === player.name) {
        const dx = msg.pos[0] - player.pos[0];
        const dy = msg.pos[1] - player.pos[1];
        const dz = msg.pos[2] - player.pos[2];
        player.pos = msg.pos;
        // Ignore sub-unit gravity drift; this only fires for real move steps.
        const dirs = [];
        if (Math.hypot(dx, dz) > 0.4) {
          dirs.push(Math.abs(dx) > Math.abs(dz)
            ? (dx > 0 ? "east" : "west")
            : (dz > 0 ? "north" : "south"));
        }
        if (Math.abs(dy) > 0.4) dirs.push(dy > 0 ? "up" : "down");
        if (dirs.length) driveVisual(dirs);
      } else {
        const mate = otherPilots[msg.from] || (otherPilots[msg.from] = {});
        const now = nowMs();
        // Estimate the wingman's drift from consecutive authoritative frames
        // (HUD-only aiming aid, D57): the server still judges every shot.
        if (mate.pos && mate.posAt) {
          const dt = Math.max(1, now - mate.posAt) / 1000;
          const nv = [
            (msg.pos[0] - mate.pos[0]) / dt,
            (msg.pos[1] - mate.pos[1]) / dt,
            (msg.pos[2] - mate.pos[2]) / dt,
          ];
          const k = 0.35; // EMA so one jumpy frame cannot spike the pip
          mate.vel = mate.vel
            ? [mate.vel[0] * (1 - k) + nv[0] * k,
               mate.vel[1] * (1 - k) + nv[1] * k,
               mate.vel[2] * (1 - k) + nv[2] * k]
            : nv;
        }
        mate.pos = msg.pos.slice();
        mate.posAt = now;
        mate.seen = now;
      }
    }
    if (msg.t === "vitals") {
      if (msg.from === player.name) {
        applyVitals(msg);
      } else {
        const mate = otherPilots[msg.from] || (otherPilots[msg.from] = {});
        mate.hp = msg.hp;
        mate.shields = msg.shields;
        mate.energy = msg.energy;
        if (msg.max_shields !== undefined) mate.max_shields = msg.max_shields;
        mate.seen = nowMs();
      }
    }
    if (msg.t === "say") {
      if (msg.to && msg.to === player.name) logChat(`Whisper from ${msg.from}: ${msg.text}`);
      else logChat(`${msg.from}: ${msg.text}`);
    }
    if (msg.t === "you" && msg.you && msg.you.message) {
      logChat(msg.you.message);
    }
    if (msg.t === "join") {
      // A squadmate joined; show them even before they move.
      if (msg.name && msg.name !== player.name) {
        const mate = otherPilots[msg.name] || (otherPilots[msg.name] = {});
        if (msg.pos) mate.pos = msg.pos.slice();
        if (msg.hp !== undefined) mate.hp = msg.hp;
        if (msg.shields !== undefined) mate.shields = msg.shields;
        if (msg.max_shields !== undefined) mate.max_shields = msg.max_shields;
        if (msg.energy !== undefined) mate.energy = msg.energy;
        mate.seen = nowMs();
        logChat(`${msg.name} joined the squad.`);
      }
    }
    if (msg.t === "leave") {
      if (msg.name && otherPilots[msg.name]) {
        delete otherPilots[msg.name];
        if (affiliate === msg.name) affiliate = null;
        logChat(`${msg.name} left the squad.`);
      }
    }
    if (msg.t === "proj") {
      // Authoritative bolts in flight (D59): replace the tracer list each tick.
      const next = {};
      for (const s of (msg.shots || [])) {
        if (s && s.id && s.pos) next[s.id] = { pos: s.pos.slice() };
      }
      projectiles = next;
    }
    if (msg.t === "combat" && msg.kind === "miss") {
      // A bolt flew past: a small whiff hint, no damage.
      sfxTone(120, 0.06, "square", 0.08, 40);
    }
    if (msg.t === "affiliate") {
      if (msg.from === player.name) affiliate = msg.to || null;
      else if (msg.to === player.name) affiliate = msg.from;
      logChat(msg.to
        ? `${msg.from} is flying with ${msg.to}.`
        : `${msg.from} is flying on their own.`);
    }
    // Mutual fight handshakes (D55): both sides of the pair see these.
    if (msg.t === "engaged") {
      const wasFighting = !!fightTarget;
      if (msg.from === player.name) fightTarget = msg.target || null;
      doEngageBtn();
      if (msg.from === player.name && fightTarget) {
        logChat(wasFighting
          ? `Rematch — the fight with ${fightTarget} is on again.`
          : "Autopilot is closing on them — press W to fly your own line.");
      }
    }
    if (msg.t === "disengaged") {
      if (msg.from === player.name) fightTarget = null;
      doEngageBtn();
    }
    if (msg.t === "portal_exit") {
      // Through the wormhole: the level is behind us.
      objectiveState = "complete";
      portalOpen = false;
      if (msg.pos) {
        player.pos = msg.pos.slice();
        renderPos = player.pos.slice();
      }
      logChat(msg.text || "You slipped through the wormhole to the exit.");
      sfxPortal();
    }
    if (msg.t === "portal_open") {
      portalOpen = true;
      sfxPortal();
      logChat(msg.text || "The wormhole is open.");
    }
    if (msg.t === "toast") {
      logChat(msg.text);
      if (msg.item) {
        worldLoot = worldLoot.filter(l => l.id !== msg.item);
        // Salvage in the hold: the objective is done.
        objectiveState = "done";
      }
    }
    if (msg.t === "mob_pos") {
      // A mob moved (it closes on the pilots that engage it).
      const mob = npcs.find(n => n.id === msg.id);
      if (mob && msg.pos) mob.pos = msg.pos.slice();
    }
    if (msg.t === "mobs") {
      // The server resends the roster when a mob dies or respawns.
      npcs = (msg.mobs || []).filter(m => m.alive !== false);
    }
    if (msg.t === "combat") {
      if (msg.kind === "shot") {
        // The server only broadcasts a shot frame when the shot landed.
        if (msg.from === player.name) showHitMarker("");
        if (msg.target === player.name) {
          // We are the target: marker + shake whether we survive or not.
          showHitMarker(msg.dead ? "dead" : "hurt");
          shakeUntil = nowMs() + 260;
          shakeMag = Math.max(shakeMag, 1);
        }
        // Keep the target's health current so its bar above the hull is live.
        const target = npcs.find(n => n.id === msg.target);
        if (target && msg.hp !== undefined) target.hp = msg.hp;
        sfxHit();
        if (msg.dead) {
          sfxKill();
          // The objective advances: the wreck now holds salvage.
          if (msg.from === player.name) objectiveState = "salvage";
          if (msg.from === player.name) {
            // Kill feedback (D57): banner + gold wash + a heavier shake.
            // The server says what was hit, so a pilot kill reads as a pilot
            // kill even when the loser has already left the roster (D58).
            const wasPilot = msg.target_kind === "pilot"
              || otherPilots[msg.target] !== undefined;
            showKillBanner(wasPilot ? "PILOT DESTROYED" : "TARGET DESTROYED");
            showKillFlash();
            shakeUntil = nowMs() + 320;
            shakeMag = Math.max(shakeMag, 1.6);
            logChat(wasPilot
              ? `You destroyed ${msg.target} in a fight.`
              : "You destroyed an enemy.");
          } else {
            logChat(`${msg.from} destroyed ${msg.target}.`);
          }
        }
      }
      if (msg.kind === "mob_windup") {
        // The guard is charging a shot: flash it so the hit is not a surprise.
        mobWindup[msg.mob] = nowMs() + 400;
        sfxTone(880, 0.08, "sine", 0.08, 660);
      }
      if (msg.kind === "mob_hit") {
        // An NPC shot landed on a pilot.
        if (msg.pilot === player.name) {
          showHitMarker("hurt");
          const shooter = npcs.find(n => n.id === msg.mob);
          if (shooter) showDamageArrow(shooter.pos);
        }
        sfxHit();
        if (msg.dead) {
          sfxDeath();
          showHitMarker("dead");
          logChat(msg.pilot === player.name
            ? "You were destroyed by an enemy."
            : `${msg.pilot} was destroyed by an enemy.`);
        }
      }
      if (msg.kind === "collision") {
        // A rock impact: bounce damage, or a head-on blast.
        if (msg.pilot === player.name) {
          if (msg.explosion) {
            logChat(`You slammed into ${msg.name} at ${msg.speed} u/s — it blew up!`);
            sfxExplosion();
            showHitMarker("dead");
          } else {
            // Bounces come in bursts while crossing the field: say it once.
            if (nowMs() - lastBounceHint > 2500) {
              lastBounceHint = nowMs();
              logChat(`You bounced off ${msg.name} for ${msg.damage}.`);
            }
            sfxImpact();
            showHitMarker("hurt");
          }
          shakeUntil = nowMs() + 320;
          shakeMag = Math.max(shakeMag, msg.explosion ? 2 : 1);
        }
      }
    }
    if (msg.t === "loot") {
      // Salvage fell where an agent died; remember it so the pilot can fly
      // over and grab it.
      if (msg.item && msg.item.id) {
        worldLoot = worldLoot.filter(l => l.id !== msg.item.id);
        worldLoot.push(Object.assign({}, msg.item));
      }
    }
    if (msg.t === "repair_result") {
      logChat(msg.success
        ? "Repair sent — your energy paid for their shields."
        : (msg.error || "Repair failed."));
    }
    if (msg.t === "fog_shared") {
      // A wingman shared their chart: keep their contacts as shared marks.
      const ttl = 20000;
      const now = nowMs();
      let n = 0;
      for (const hit of (msg.hits || [])) {
        if (hit.kind !== "debris") continue;
        sharedBlips[hit.id] = { until: now + ttl, mass: hit.mass || 0 };
        n++;
      }
      logChat(`${msg.from} shared their chart — ${n} contact(s).`);
    }
    if (msg.t === "scan_result") {
      if (msg.ok) {
        const ttl = Number(msg.ttl) || 4000;
        const now = nowMs();
        const hits = msg.hits || [];
        for (const hit of hits) {
          if (hit.kind === "debris") {
            scanBlips[hit.id] = now + ttl;
            scanMass[hit.id] = hit.mass || 0;
          }
        }
        logChat(hits.length
          ? `Scan complete — ${hits.length} contact(s).`
          : "Scan complete — nothing in range. Fly toward the debris field.");
        for (const hit of hits) {
          if (hit.kind !== "debris") continue;
          logChat(`  ${hit.name} — ${weightClass(hit.mass)}`);
        }
      } else {
        logChat(msg.reason || "Scan failed.");
      }
    }
    if (msg.t === "debris_gone") {
      const d = debris.find(x => x.id === msg.id);
      if (d) d.harvested = true;
      if (msg.explosion) {
        // A rock was blown apart — it leaves the world for everyone.
        sfxExplosion();
      } else if (msg.from === player.name) {
        applyVitals(msg.vitals);
        nearDebris = null;
        sfxHarvest();
      }
    }
  };
  ws.onerror = () => console.error("WS error");
  ws.onclose = () => console.log("WS closed");
}

// ── init on load ────────────────────────────────────────────────────────

function init() {
  initGL();
  initQuadBuf();
  initCube();
  initWormhole();
  initFighter();
  initAsteroid();
  initStars();
  initMiniMap();
  // Wire the sky-bloom program once (degrades to single-pass on failure).
  enableBloom();
  hudEl = document.querySelector("#cockpit-hud");
  helpEl = document.querySelector("#help");
  tacticalCanvas = document.querySelector("#tactical-map");
  const fsBtn = document.querySelector("#fullscreen-btn");
  if (fsBtn) fsBtn.addEventListener("click", toggleFullscreen);

  const fightBtn = document.querySelector("#fight-btn");
  if (fightBtn) fightBtn.addEventListener("click", () => {
    if (fightTarget) doDisengage();
    else doEngage();
  });

  const params = new URLSearchParams(window.location.search);

  // Auth gate: a pilot must sign in (or register) before flying. The token
  // is kept in localStorage so a reload does not drop the session.
  async function doAuth(mode) {
    const name = (document.querySelector("#auth-name").value || "").trim();
    const password = document.querySelector("#auth-pass").value || "";
    const status = document.querySelector("#auth-status");
    if (!name || !password) { status.textContent = "name and password required"; return; }
    try {
      const r = await fetch("/api/auth", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, password }),
      });
      const data = await r.json();
      if (!data.ok) { status.textContent = data.error || "auth failed"; return; }
      localStorage.setItem("qfe_token", data.token);
      localStorage.setItem("qfe_name", data.name);
      // Hide the panel and start the game.
      const panel = document.querySelector("#auth-panel");
      if (panel) panel.style.display = "none";
      player.name = data.name;
      if (!ws || ws.readyState === WebSocket.CLOSED) connectWS(data.name, data.token);
    } catch (e) {
      status.textContent = "network error";
    }
  }
  const loginBtn = document.querySelector("#login-btn");
  const registerBtn = document.querySelector("#register-btn");
  if (loginBtn) loginBtn.addEventListener("click", () => doAuth("login"));
  if (registerBtn) registerBtn.addEventListener("click", () => doAuth("register"));
  const nameField = document.querySelector("#auth-name");
  const passField = document.querySelector("#auth-pass");
  if (passField) passField.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doAuth("login");
  });

  // Auto-resume a stored session; otherwise send the pilot to the login page.
  const storedToken = localStorage.getItem("qfe_token");
  const storedName = localStorage.getItem("qfe_name");
  if (storedToken && storedName) {
    player.name = storedName;
    connectWS(storedName, storedToken);
  } else {
    window.location = "/login";
    return;
  }

  const chatInput = document.querySelector("#chat-input");
  if (chatInput) chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ t: "chat", name: player.name, text: chatInput.value }));
      chatInput.value = "";
    }
  });

  document.addEventListener("keydown", handleKeyDown);
  document.addEventListener("keyup", handleKeyUp);
  document.addEventListener("mousemove", handleMouseMove);
  document.addEventListener("pointerlockchange", () => {
    pointerLocked = document.pointerLockElement === canvas;
  });
  window.addEventListener("blur", stopThrust);
  if (canvas) canvas.addEventListener("click", () => {
    // Fire on every click so the gun works even when pointer lock is
    // unavailable (e.g. an embedded/iframe context). Lock the pointer too
    // when possible so mouse-look keeps working.
    doFire();
    if (document.pointerLockElement !== canvas && canvas.requestPointerLock) {
      try { canvas.requestPointerLock(); } catch (e) { /* ignore */ }
    }
  });

  initCamera();  // fallback until the authoritative portal arrives

  // Feedback button wiring.
  const fbBtn = document.querySelector("#feedback-btn");
  const fbPanel = document.querySelector("#feedback-panel");
  const fbText = document.querySelector("#feedback-text");
  const fbSend = document.querySelector("#feedback-send");
  const fbCancel = document.querySelector("#feedback-cancel");
  const fbStatus = document.querySelector("#feedback-status");
  if (fbBtn && fbPanel) {
    fbBtn.addEventListener("click", () => {
      fbPanel.style.display = fbPanel.style.display === "none" ? "block" : "none";
      if (fbStatus) fbStatus.textContent = "";
    });
  }
  if (fbCancel && fbPanel) {
    fbCancel.addEventListener("click", () => { fbPanel.style.display = "none"; });
  }
  if (fbSend) {
    fbSend.addEventListener("click", async () => {
      const note = (fbText && fbText.value || "").trim();
      if (!note) { if (fbStatus) fbStatus.textContent = "empty note"; return; }
      try {
        const r = await fetch("/api/feedback", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note, name: player.name }),
        });
        const data = await r.json();
        if (data.ok) {
          if (fbStatus) fbStatus.textContent = "sent — thanks";
          if (fbText) fbText.value = "";
        } else {
          if (fbStatus) fbStatus.textContent = data.error || "failed";
        }
      } catch (e) {
        if (fbStatus) fbStatus.textContent = "network error";
      }
    });
  }

  // One draw pass. Kept separate from the frame scheduling so a fallback
  // timer can drive the same work.
  function drawOnce() {
    // A throw inside the draw path would otherwise kill the animation frame
    // chain for good and freeze the game on a black screen. Keep the loop
    // alive and report the failure instead.
    try {
      drawScene();
      checkPortalHint();
      checkDebrisHint();
    } catch (e) {
      console.error("render error", e);
    }
  }

  let framesRendered = 0;
  function render() {
    framesRendered++;
    drawOnce();
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);
  // Some environments never fire animation frames (headless browsers, a tab
  // that never gets a compositor). Keep the view alive with a timer instead
  // of showing a frozen black screen.
  setTimeout(function watchFrames() {
    if (framesRendered > 0) return;
    setInterval(() => {
      if (framesRendered > 0) return; // animation frames took over
      drawOnce();
    }, 33);
  }, 1500);
}

// bootstrap
window.addEventListener ? window.addEventListener("load", init) : init();
