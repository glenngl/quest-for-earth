/*
  Quest for Earth — Level 1 (3-D cube, Portal, fog of war, mini-map).
  No framework, no build step.  Works in the browser alone.

  Controls:
    WASD     — move
    Mouse    — look
    Touch    — on-screen joystick
    Gamepad — left stick move, right stick look, A button action
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
// Differential engine glow while turning: +1 = turning left (right brighter).
let turnBiasTarget = 0;
let biasUntil = 0;
let engineBias = 0;
// Nose-tip vertical jets: +1 = climbing (underside fires).
let verticalTarget = 0;
let vertUntil = 0;
let verticalThrust = 0;
const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

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

// NPCs (enemies around portal)
let npcs = [];
let npcCount = 8; // starts at player count, will match

// Harvestable debris between spawn and the wormhole.
let debris = [];

// Other pilots, learned from authoritative pos/vitals frames. The tactical
// HUD uses these so a squad can coordinate attacks from different locations.
let otherPilots = {};

// Fire range learned from the welcome frame; the server still judges shots.
let fireRange = 24;

// D25: ship stores are authoritative; welcome/vitals frames fill these in.
let vitals = {
  hp: 100, shields: 100, max_hp: 100, max_shields: 100,
  energy: 100, methane: 0, metal: 0,
  max_energy: 100, max_methane: 100, max_metal: 100,
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

// Fog of war: a simple depth buffer / visibility mask
let fogMask = new Float32Array(64 * 64 * 64 * 4); // one byte per voxel × RGBA

// Mini-map canvas
let miniCanvas, miniGl, miniProgram;
let miniProjMatrix;

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

// ── init WebGL ─────────────────────────────────────────────────────────

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

// The cockpit is deliberately minimal: a thin canopy rim and a small
// gunsight. All information lives in the fixed-zone HTML HUD, so the pilot
// sees mostly the space outside the ship (D20/D27).
const cockpitFrame = [
  { p: [ 0,    1.12, 2.20], h: [0.92, 0.018, 0.018], c: [0.13, 0.16, 0.19] }, // top rim
  { p: [-0.91, 0.70, 2.20], h: [0.018, 0.42, 0.018], c: [0.13, 0.16, 0.19] }, // port
  { p: [ 0.91, 0.70, 2.20], h: [0.018, 0.42, 0.018], c: [0.13, 0.16, 0.19] }, // starboard
  { p: [ 0,    0.28, 2.20], h: [0.92, 0.018, 0.018], c: [0.13, 0.16, 0.19] }, // lower rim
];
const cockpitGlow = [
  // Gunsight: four small bars around the forward view.
  { p: [ 0, 0.80, 2.25], h: [0.09, 0.006, 0.006], c: [0.20, 0.80, 0.70, 0.5] },
  { p: [ 0, 0.94, 2.25], h: [0.09, 0.006, 0.006], c: [0.20, 0.80, 0.70, 0.5] },
  { p: [-0.06, 0.87, 2.25], h: [0.006, 0.05, 0.006], c: [0.20, 0.80, 0.70, 0.5] },
  { p: [ 0.06, 0.87, 2.25], h: [0.006, 0.05, 0.006], c: [0.20, 0.80, 0.70, 0.5] },
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
  for (const part of cockpitFrame) drawBoxPart(placement, part, 0);
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

function drawScene() {
  resize();
  gl.useProgram(program);

  // Set projection
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_proj"),
    false, projMatrix);

  // Smooth the visual heading/pitch toward the free-look orientation.
  let yawDiff = viewYaw - shipYaw;
  while (yawDiff > Math.PI) yawDiff -= 2 * Math.PI;
  while (yawDiff < -Math.PI) yawDiff += 2 * Math.PI;
  shipYaw += yawDiff * 0.18;
  shipPitch += (viewPitch - shipPitch) * 0.18;
  const frameNow = nowMs();
  thrust = Math.max(0, Math.min(1, (thrustUntil - frameNow) / 180));
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
  mat4.lookAt(viewMatrix, eye, focus, [0, 1, 0]);
  gl.uniformMatrix4fv(
    gl.getUniformLocation(program, "u_view"),
    false, viewMatrix);

  // Clear
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0.02, 0.02, 0.04, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);

  // Far end = cool entrance, spawn = warm exit. Both spin in opposite
  // directions so a pilot can tell them apart at a glance.
  drawWormhole(portal.pos[0], portal.pos[1], portal.pos[2], 5, false);
  drawWormhole(spawnPortal.pos[0], spawnPortal.pos[1], spawnPortal.pos[2], 3, true);

  // Draw the harvestable debris field between spawn and the wormhole
  drawDebris();

  // Once the camera is in the cockpit the hull is behind us (or would be
  // inside the near plane), so stop drawing the ship.
  if (camZoom > 2.5) {
    drawFighter(player.pos[0], player.pos[1] + 1.2, player.pos[2], 1.25, shipYaw, shipPitch);
  } else {
    drawCockpit(mat4.modelYawPitch(player.pos[0], player.pos[1] + 1.2,
                                   player.pos[2], 1.25, shipYaw, shipPitch));
  }

  // Draw NPCs (enemies) as red cubes around portal
  for (let npc of npcs) {
    drawCube(npc.pos[0], npc.pos[1], npc.pos[2], 1.5, [1, 0, 0, 0.9]);
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

function drawDebris() {
  if (!rockBuffer || !crystalBuffer) return;
  const loc = gl.getAttribLocation(program, "a_position");
  const uModel = gl.getUniformLocation(program, "u_model");
  const uColor = gl.getUniformLocation(program, "u_color");
  gl.uniform1f(emissiveLoc, 0);

  // Crystal colour is the only thing that changes between the three types.
  const styles = {
    asteroid:    { scale: 1.65, crystal: [0.42, 0.30, 0.19, 1], rock: [0.19, 0.18, 0.17, 1] },
    ice_chunk:   { scale: 0.92, crystal: [0.25, 0.85, 1.00, 0.92], rock: [0.14, 0.21, 0.26, 1] },
    scrap_metal: { scale: 0.86, crystal: [0.78, 0.83, 0.88, 1], rock: [0.20, 0.22, 0.24, 1] },
  };
  for (const d of debris) {
    if (d.harvested) continue;
    const [x, y, z] = d.pos;
    const angle = (parseInt(String(d.id).replace(/\D/g, ""), 10) || 0) * 0.7;
    const s = styles[d.type] || styles.asteroid;
    gl.uniformMatrix4fv(uModel, false, mat4.modelRotZ(x, y, z, s.scale, angle));

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
    ctx.strokeStyle = "rgba(255,210,127,0.5)";
    ctx.beginPath();
    ctx.moveTo(px, py); ctx.lineTo(mx, my); ctx.stroke();
    ctx.fillStyle = "rgba(255,210,127,0.95)";
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

  // Draw portal at its authoritative (x,z) position on the mini-map
  const portalX = 10 + portal.pos[0] * scale;
  const portalY = 10 + (63 - portal.pos[2]) * scale;
  ctx.fillStyle = "rgba(255,255,0,0.9)";
  ctx.beginPath();
  ctx.arc(portalX, portalY, 4, 0, Math.PI * 2);
  ctx.fill();

  // Draw fog: overlay dark semi-transparent where unseen
  // (In a full impl this would use the fogMask; here we just darken the whole thing slightly)
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

// ── cockpit HUD and fullscreen ─────────────────────────────────────────
// Fixed zones: squad top-centre, own ship bottom-left, tactical map
// bottom-right. The eye always finds the same information in the same place.
let hudEl = null;
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
  const minimap = document.querySelector("#minimap");
  if (minimap) minimap.style.display = firstPerson ? "none" : "block";
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
      line(name, `${vitalsText} · ${dist.toFixed(0)}u`, "#ffd27f");
    }
  }
  drawTacticalMap();
}

function drawTacticalMap() {
  if (!tacticalCanvas) return;
  const ctx = tacticalCanvas.getContext("2d");
  if (!ctx) return;
  const S = tacticalCanvas.width;
  ctx.clearRect(0, 0, S, S);
  ctx.fillStyle = "#04171c";
  ctx.fillRect(0, 0, S, S);
  const pad = 12;
  const span = S - pad * 2;
  const px = (wx) => pad + (wx / 63) * span;
  const py = (wz) => S - pad - (wz / 63) * span; // +z points up

  ctx.strokeStyle = "rgba(110,220,230,0.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(pad, pad, span, span);
  ctx.beginPath();
  ctx.moveTo(pad + span / 2, pad); ctx.lineTo(pad + span / 2, S - pad);
  ctx.moveTo(pad, pad + span / 2); ctx.lineTo(S - pad, pad + span / 2);
  ctx.stroke();

  // Bearing lines link the flight leader to each teammate for coordination.
  for (const mate of Object.values(otherPilots)) {
    if (!mate.pos) continue;
    ctx.strokeStyle = "rgba(255,210,127,0.5)";
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
    const c = d.material === "rock" ? "#8a6a45"
            : d.material === "methane" ? "#48ccff" : "#c8ced4";
    marker(d.pos[0], d.pos[2], c, 3);
  }
  for (const npc of npcs) marker(npc.pos[0], npc.pos[2], "#ff5555", 4);
  marker(spawnPortal.pos[0], spawnPortal.pos[2], "#ff8a3c", 6);
  marker(portal.pos[0], portal.pos[2], "#ffe34d", 7);
  for (const mate of Object.values(otherPilots)) {
    if (mate.pos) marker(mate.pos[0], mate.pos[2], "#ffd27f", 6);
  }
  marker(player.pos[0], player.pos[2], "#7dff9b", 6);
}

// ── input handling ─────────────────────────────────────────────────────

function handleKeyDown(e) {
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  const key = e.key.toLowerCase();
  // Simple forward/back/left/right based on current look direction
  // For Level 1 proto, we'll just update pos based on absolute directions
  // (the server will validate/rehash; client just sends intent)
  if (key === "v") { toggleCamera(); return; }
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

  // W/S thrust along the current view vector; pitch down and hold W to dive
  // toward the spawn exit. A/D and Space/Shift remain absolute-axis moves.
  if (key === "w") { thrustHeld = 1; startThrust(); }
  else if (key === "s") { thrustHeld = -1; startThrust(); }
  else if (key === "a") engine_tick_move("west");
  else if (key === "d") engine_tick_move("east");
  else if (key === " ") engine_tick_move("up");
  else if (key === "shift") engine_tick_move("down");
  else if (key === "e" && ws && ws.readyState === WebSocket.OPEN) {
    // Intent only — the server decides whether we're on the valid side.
    ws.send(JSON.stringify({ t: "enter_portal", name: player.name }));
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

// Closest harvestable chunk within reach, for the F key and the hint.
let nearDebris = null;
let lastHarvestHint = null;
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
  if (inZone && !nearPortal) {
    nearPortal = true;
    logChat("Wormhole ahead — press E to enter.");
  } else if (!inZone) {
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
function nearestFireTarget() {
  let best = null;
  let bestDist = Infinity;
  const consider = (id, pos) => {
    const dist = Math.hypot(pos[0] - player.pos[0], pos[1] - player.pos[1], pos[2] - player.pos[2]);
    if (dist > fireRange) return;
    if (dist < bestDist) { bestDist = dist; best = id; }
  };
  for (const [name, mate] of Object.entries(otherPilots)) {
    if (mate.pos) consider(name, mate.pos);
  }
  for (const npc of npcs) {
    if (npc && npc.pos) consider(npc.id, npc.pos);
  }
  return best;
}

function doFire() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const target = nearestFireTarget();
  if (!target) { logChat("No target in range — fly into the fight."); return; }
  ws.send(JSON.stringify({ t: "fire", target, name: player.name }));
}

// ── WebSocket connection ────────────────────────────────────────────────

let ws = null;

function logChat(text) {
  const log = document.querySelector("#chat-log");
  if (log) log.innerHTML += `<div class="msg">${text}</div>`;
  const chat = document.querySelector("#chat");
  if (chat) chat.scrollTop = chat.scrollHeight;
}

function applyVitals(v) {
  if (!v) return;
  for (const key of ["hp", "shields", "max_hp", "max_shields", "energy",
                     "methane", "metal", "max_energy", "max_methane", "max_metal"]) {
    if (v[key] !== undefined) vitals[key] = v[key];
  }
  player.hp = vitals.hp;
  player.shields = vitals.shields;
}

function connectWS(name) {
  player.name = name;
  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    console.log("WS connected");
    ws.send(JSON.stringify({ t: "set_name", name }));
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
      if (msg.portal) {
        portal.pos = msg.portal.pos;
        portal.front = msg.portal.front || "-z";
        portal.aperture = msg.portal.aperture || 4;
      }
      if (msg.spawn_portal) spawnPortal = Object.assign({}, spawnPortal, msg.spawn_portal);
      initCamera();
      fireRange = msg.fire_range || 24;
      logChat(`Welcome, ${player.name}.`);
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
        mate.pos = msg.pos.slice();
        mate.seen = nowMs();
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
        mate.seen = nowMs();
      }
    }
    if (msg.t === "say") {
      logChat(`${msg.from}: ${msg.text}`);
    }
    if (msg.t === "you" && msg.you && msg.you.message) {
      logChat(msg.you.message);
    }
    if (msg.t === "toast") {
      logChat(msg.text);
    }
    if (msg.t === "combat") {
      if (msg.kind === "shot") logChat(`${msg.from} hit ${msg.target} for ${msg.damage}.`);
      if (msg.kind === "mob_hit" && msg.hit) logChat(`${msg.pilot} destroyed an enemy.`);
    }
    if (msg.t === "debris_gone") {
      const d = debris.find(x => x.id === msg.id);
      if (d) d.harvested = true;
      if (msg.from === player.name) {
        applyVitals(msg.vitals);
        nearDebris = null;
      }
    }
  };
  ws.onerror = () => console.error("WS error");
  ws.onclose = () => console.log("WS closed");
}

// ── init on load ────────────────────────────────────────────────────────

function init() {
  initGL();
  initCube();
  initWormhole();
  initFighter();
  initAsteroid();
  initMiniMap();
  hudEl = document.querySelector("#cockpit-hud");
  tacticalCanvas = document.querySelector("#tactical-map");
  const fsBtn = document.querySelector("#fullscreen-btn");
  if (fsBtn) fsBtn.addEventListener("click", toggleFullscreen);

  const field = document.querySelector("#name-field");
  const startBtn = document.querySelector("#start-btn");
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("name");
  if (requested && field) field.value = requested;

  const start = () => {
    const name = (field && field.value.trim()) || requested || "Player";
    if (!ws || ws.readyState === WebSocket.CLOSED) connectWS(name);
  };
  if (startBtn) startBtn.addEventListener("click", start);
  if (field) field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") start();
  });
  // Auto-join using the field value (or ?name=) — the button is for changing name.
  start();

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
    if (document.pointerLockElement === canvas) {
      // Locked on: the click is the shot (prototype direct fire).
      doFire();
    } else if (canvas.requestPointerLock) {
      canvas.requestPointerLock();
    }
  });

  initCamera();  // fallback until the authoritative portal arrives

  function render() {
    drawScene();
    checkPortalHint();
    checkDebrisHint();
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);
}

// bootstrap
window.addEventListener ? window.addEventListener("load", init) : init();
