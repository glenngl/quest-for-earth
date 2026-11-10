/* Return to Mordor — browser client.
 *
 * Renders the server-authoritative game (D2): the WebSocket stream is the
 * only source of truth. Scene = three.js (vendored, no build step, D5):
 * a torch-lit room box rebuilt on every move, characters as low-poly
 * capsules with nameplates, combat as tweens (lunges, flashes, damage
 * popups, vigor sparkles). Touch-first: drag to look, buttons to act.
 */

import * as THREE from "./vendor/three.module.js";

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CLASS_COLORS = {
  warrior: 0xc0392b, ranger: 0x2e8b57, thief: 0x8e44ad,
  mage: 0x2980b9, cleric: 0xf1c40f,
};
const MOB_COLORS = [0xa13d2d, 0x6b8e23, 0x8a5a2b, 0x74502c];

/* ================================ state ================================ */

let token = localStorage.getItem("r2m_token") || null;
let you = null;
let roomView = null;
let ws = null;
let wsAlive = false;
let fighting = false;
let targetId = null;      // mob instance id for attacks
let healTargetName = null; // teammate selected for vigor

/* ================================ auth ================================ */

let authMode = "login";
$("#tab-login").onclick = () => setAuthMode("login");
$("#tab-register").onclick = () => setAuthMode("register");

function setAuthMode(mode) {
  authMode = mode;
  for (const el of document.querySelectorAll(".tab")) el.classList.remove("active");
  $(mode === "login" ? "#tab-login" : "#tab-register").classList.add("active");
  $("#auth-error").textContent = "";
}

$("#auth-submit").onclick = async () => {
  $("#auth-error").textContent = "";
  const path = authMode === "login" ? "/api/login" : "/api/register";
  try {
    const res = await api(path, {
      username: $("#auth-username").value.trim(),
      password: $("#auth-password").value,
    });
    token = res.token;
    localStorage.setItem("r2m_token", token);
    res.character ? showGame() : showCreate();
  } catch (exc) {
    $("#auth-error").textContent = exc.message;
  }
};

$("#auth-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#auth-submit").click();
});

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json", "X-Token": token } : { "X-Token": token },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

/* ======================== character creation ========================== */

const races = [];
let classesCatalog = { primary: [] };

async function showCreate() {
  setView("create");
  if (!races.length) {
    const [r, c] = await Promise.all([api("/api/races"), api("/api/classes")]);
    races.push(...r.races);
    classesCatalog = c;
    for (const race of races) {
      $("#create-race").add(new Option(`${race.name} (${race.size})`, race.name));
    }
    for (const cls of classesCatalog.primary) {
      $("#create-class1").add(new Option(cls.name, cls.id));
      $("#create-class2").add(new Option(cls.name, cls.id));
    }
    $("#create-class2").add(new Option("(none yet)", ""));
    $("#create-race").onchange = updateStatPreview;
    $("#create-class1").onchange = updateStatPreview;
    updateStatPreview();
  }
}

function updateStatPreview() {
  const race = races.find((r) => r.name === $("#create-race").value);
  if (!race) return;
  const cls1 = classesCatalog.primary.find((c) => c.id === $("#create-class1").value);
  const stats = { ...race.stats };
  if (cls1) stats[cls1.primary_stat] += 1;
  for (const key of ["str", "int", "wis", "dex", "con"]) {
    $(`#stat-${key}`).textContent = stats[key];
  }
  const cls2 = classesCatalog.primary.find((c) => c.id === $("#create-class2").value);
  $("#create-blurb").textContent =
    (cls1 ? `${cls1.name} — ${cls1.blurb} ` : "") +
    (cls2 && $("#create-class2").value ? `${cls2.name} — ${cls2.blurb}` : "");
}

$("#create-submit").onclick = async () => {
  $("#create-error").textContent = "";
  try {
    await api("/api/character", {
      name: $("#create-name").value.trim(),
      gender: $("#create-gender").value,
      race: $("#create-race").value,
      class1: $("#create-class1").value,
      class2: $("#create-class2").value || classesCatalog.primary[0].id,
    });
    showGame();
  } catch (exc) {
    $("#create-error").textContent = exc.message;
  }
};

/* ============================== websocket ============================= */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => { wsAlive = true; };
  ws.onclose = () => {
    wsAlive = false;
    if (document.body.classList.contains("view-game")) {
      toast("connection lost — reconnecting…");
      setTimeout(connect, 2500);
    }
  };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

function handle(msg) {
  switch (msg.t) {
    case "welcome":
      you = msg.you;
      updateSelf();
      buildRoom(msg.room);
      logLine(`You are in ${msg.room.name}.`, "muted");
      break;
    case "room":
      goRoom(msg.room);
      break;
    case "enter":
      if (msg.who.name !== you.name) {
        addActor(msg.who);
        toast(`${msg.who.name} arrives.`);
        logLine(`${msg.who.name} arrives.`);
      }
      break;
    case "leave":
      removeActorByName(msg.who);
      logLine(`${msg.who} leaves.`, "muted");
      break;
    case "combat":
      renderEvents(msg.events);
      break;
    case "say":
      toast(`${msg.from}: ${msg.text}`);
      logLine(`${msg.from} says: ${msg.text}`);
      break;
    case "toast":
      toast(msg.text);
      logLine(msg.text, "muted");
      break;
    case "you":
      you = msg.you;
      updateSelf();
      break;
    case "error":
      toast(msg.text);
      if (msg.text.includes("create a character")) showCreate();
      break;
  }
}

/* ============================== three.js ============================== */

const canvas = $("#scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(72, 1, 0.1, 60);
scene.add(new THREE.AmbientLight(0xffffff, 1.45));

const CAM_R = 5.4, CAM_H = 1.75;
let camYaw = 0;
let camBob = 0;

let roomGroup = null;
let torches = [];
const tweens = [];
const actors = new Map();   // id -> {group, body, info}
const SLOTS = [[-2.4, 2.6], [2.4, 2.6], [-2.4, -2.6], [2.4, -2.6],
               [0, 3.1], [0, -3.1], [-3.8, 0], [3.8, 0]];

function onResize() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", onResize);
onResize();

function disposeRoom() {
  if (!roomGroup) return;
  roomGroup.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      for (const m of [].concat(obj.material)) {
        if (m.map) m.map.dispose();
        m.dispose();
      }
    }
  });
  scene.remove(roomGroup);
  roomGroup = null;
  actors.clear();
  torches = [];
  tweens.length = 0;
}

function buildRoom(view) {
  roomView = view;
  disposeRoom();
  roomGroup = new THREE.Group();

  const base = new THREE.Color(view.tint || "#1a1f2e");
  scene.background = base.clone().addScalar(0.02);
  scene.fog = new THREE.Fog(base.clone().addScalar(0.015), 7, 21);

  const floorMat = new THREE.MeshLambertMaterial({ color: base.clone().addScalar(0.08) });
  const wallMat = new THREE.MeshLambertMaterial({ color: base.clone().addScalar(0.05) });

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), floorMat);
  floor.rotation.x = -Math.PI / 2;
  roomGroup.add(floor);

  for (const dir of ["north", "south", "east", "west"]) {
    buildSide(dir, Boolean(view.exits[dir]), wallMat, floorMat);
  }

  torch(-4.3, -4.3);
  torch(4.3, 4.3);
  scene.add(roomGroup);
  populateActors(view);
  updateCamera();
}

function buildSide(dir, hasExit, wallMat, floorMat) {
  const S = 6, GAP = 3.0, WALL_H = 3.2, THICK = 0.4;
  const isNS = dir === "north" || dir === "south";
  const sign = dir === "north" ? -1 : dir === "south" ? 1 : dir === "east" ? 1 : -1;
  if (!hasExit) {
    const wall = new THREE.Mesh(
      isNS ? new THREE.BoxGeometry(12, WALL_H, THICK)
           : new THREE.BoxGeometry(THICK, WALL_H, 12), wallMat);
    if (isNS) wall.position.set(0, WALL_H / 2, S * sign);
    else wall.position.set(S * sign, WALL_H / 2, 0);
    roomGroup.add(wall);
    return;
  }
  const segLen = (12 - GAP) / 2;
  for (const p of [-(GAP / 2 + segLen / 2), GAP / 2 + segLen / 2]) {
    const wall = new THREE.Mesh(
      isNS ? new THREE.BoxGeometry(segLen, WALL_H, THICK)
           : new THREE.BoxGeometry(THICK, WALL_H, segLen), wallMat);
    if (isNS) wall.position.set(p, WALL_H / 2, S * sign);
    else wall.position.set(S * sign, WALL_H / 2, p);
    roomGroup.add(wall);
  }
  if (hasExit) {
    const lintel = new THREE.Mesh(
      isNS ? new THREE.BoxGeometry(GAP, WALL_H - 2.3, THICK)
           : new THREE.BoxGeometry(THICK, WALL_H - 2.3, GAP), wallMat);
    lintel.position.y = 2.3 + (WALL_H - 2.3) / 2;
    if (isNS) lintel.position.z = S * sign; else lintel.position.x = S * sign;
    roomGroup.add(lintel);

    const patch = new THREE.Mesh(
      isNS ? new THREE.PlaneGeometry(GAP, 2.4) : new THREE.PlaneGeometry(2.4, GAP),
      floorMat);
    patch.rotation.x = -Math.PI / 2;
    if (isNS) patch.position.set(0, 0.01, S * sign + 1.2 * sign);
    else patch.position.set(S * sign + 1.2 * sign, 0.01, 0);
    roomGroup.add(patch);
  }
}

function torch(x, z) {
  const group = new THREE.Group();
  const pole = new THREE.Mesh(
    new THREE.CylinderGeometry(0.04, 0.06, 1.1, 6),
    new THREE.MeshLambertMaterial({ color: 0x3a2e22 }));
  pole.position.y = 0.55;
  const flame = new THREE.Mesh(
    new THREE.ConeGeometry(0.13, 0.32, 6),
    new THREE.MeshBasicMaterial({ color: 0xffa540 }));
  flame.position.y = 1.28;
  const light = new THREE.PointLight(0xff9a3c, 11, 12, 2);
  light.position.y = 1.6;
  group.add(pole, flame, light);
  group.position.set(x, 0, z);
  group.userData = { phase: Math.random() * 6.3 };
  torches.push({ light, flame });
  roomGroup.add(group);
}

/* --------------------------- actors ---------------------------------- */

function nameplate(text, color) {
  const c = document.createElement("canvas");
  c.width = 256; c.height = 56;
  const ctx = c.getContext("2d");
  ctx.font = "600 30px system-ui, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.shadowColor = "#000"; ctx.shadowBlur = 6;
  ctx.fillStyle = color;
  ctx.fillText(text, 128, 30);
  const tex = new THREE.CanvasTexture(c);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  sprite.scale.set(2.1, 0.46, 1);
  return sprite;
}

function addActor(info, slotIndex) {
  if (info.kind === "player" && info.name === you.name) return;
  if (actors.has(String(info.id))) return;
  const color = info.kind === "mob"
    ? MOB_COLORS[(info.name.length + info.name.charCodeAt(0)) % MOB_COLORS.length]
    : CLASS_COLORS[info.class1] || 0x95a5a6;
  const group = new THREE.Group();
  const body = new THREE.Mesh(
    new THREE.CapsuleGeometry(0.32, 0.75, 6, 12),
    new THREE.MeshLambertMaterial({ color, transparent: true }));
  body.position.y = 0.75;
  const head = new THREE.Mesh(
    new THREE.SphereGeometry(0.21, 12, 10),
    new THREE.MeshLambertMaterial({ color: 0xd8b090, transparent: true }));
  head.position.y = 1.38;
  const plate = nameplate(info.name, info.kind === "mob" ? "#f0a08a" : "#d8dee9");
  plate.position.y = 2.0;
  group.add(body, head, plate);
  const slot = slotIndex ?? actors.size;
  const s = SLOTS[slot % SLOTS.length];
  group.position.set(s[0], 0, s[1]);
  group.userData = { phase: Math.random() * 6.3, walking: false };
  actors.set(String(info.id), { group, body, head, plate, info });
  roomGroup.add(group);
}

function populateActors(view) {
  actors.clear();
  view.players.forEach((p, i) => addActor(p, i));
  view.mobs.forEach((m, i) => addActor(m, i + view.players.length));
  const online = view.players.length;
  $("#online").textContent = `${online} in room`;
}

function addActorFromEvent(info) {
  const slot = actors.size;
  addActor(info, slot);
}

function removeActorByName(name) {
  for (const [id, a] of actors) {
    if (a.info.name === name) {
      roomGroup.remove(a.group);
      actors.delete(id);
      if (healTargetName === name) healTargetName = null;
    }
  }
}

function actorByName(name) {
  for (const a of actors.values()) if (a.info.name === name) return a;
  return null;
}

function actorById(id) {
  return actors.get(String(id)) || null;
}

/* --------------------------- tweens & fx ------------------------------ */

function tween(dur, fn, done) {
  tweens.push({ t0: performance.now(), dur: dur * 1000, fn, done });
}

function runTweens(now) {
  for (let i = tweens.length - 1; i >= 0; i--) {
    const tw = tweens[i];
    const p = Math.min(1, (now - tw.t0) / tw.dur);
    tw.fn(p);
    if (p >= 1) {
      tweens.splice(i, 1);
      tw.done && tw.done();
    }
  }
}

function popup(actor, text, color, big) {
  if (!actor) return;
  const c = document.createElement("canvas");
  c.width = 256; c.height = 80;
  const ctx = c.getContext("2d");
  ctx.font = `${big ? "800 64px" : "700 44px"} system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.shadowColor = "#000"; ctx.shadowBlur = 8;
  ctx.fillStyle = color;
  ctx.fillText(text, 128, 42);
  const tex = new THREE.CanvasTexture(c);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  sprite.scale.set(big ? 1.7 : 1.15, big ? 0.53 : 0.36, 1);
  const from = actor.group.position.clone();
  sprite.position.set(from.x, from.y + 1.7, from.z);
  roomGroup.add(sprite);
  tween(0.9, (p) => {
    sprite.position.y = from.y + 1.7 + p * 1.15;
    sprite.material.opacity = 1 - p;
  }, () => {
    roomGroup.remove(sprite);
    tex.dispose(); sprite.material.dispose();
  });
}

function flash(actor, hex, dur = 0.35) {
  if (!actor) return;
  const color = new THREE.Color(hex);
  tween(dur, (p) => {
    const f = 1 - p;
    actor.body.material.emissive.setRGB(color.r * f, color.g * f, color.b * f);
    actor.head.material.emissive.setRGB(color.r * f * 0.4, color.g * f * 0.4, color.b * f * 0.4);
  });
}

function lunge(attacker, victim) {
  if (!attacker || !victim) return;
  const from = attacker.group.position.clone();
  const delta = victim.group.position.clone().sub(from);
  delta.y = 0;
  const reach = delta.clone().normalize().multiplyScalar(Math.min(0.85, delta.length() * 0.45));
  attacker.group.userData.walking = true;
  tween(0.5, (p) => {
    attacker.group.position.copy(from).addScaledVector(reach, Math.sin(p * Math.PI));
    if (p === 1) attacker.group.userData.walking = false;
  });
}

function vigorBurst(target, amount) {
  if (!target) return;
  flash(target, 0x22aa55, 0.7);
  popup(target, `+${amount}`, "#2ecc71");
  for (let i = 0; i < 12; i++) {
    const c = document.createElement("canvas");
    c.width = 32; c.height = 32;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#2ecc71";
    ctx.beginPath(); ctx.arc(16, 16, 5, 0, 7); ctx.fill();
    const tex = new THREE.CanvasTexture(c);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
    sprite.scale.set(0.22, 0.22, 1);
    const base = target.group.position;
    const off = new THREE.Vector3((Math.random() - 0.5) * 0.9, 0.4 + Math.random() * 1.6,
                                  (Math.random() - 0.5) * 0.9);
    sprite.position.copy(base).add(off);
    roomGroup.add(sprite);
    const dur = 0.7 + Math.random() * 0.5;
    tween(dur, (p) => {
      sprite.position.y += 0.016;
      sprite.material.opacity = 1 - p;
    }, () => {
      roomGroup.remove(sprite);
      tex.dispose(); sprite.material.dispose();
    });
  }
}

/* --------------------------- combat events ---------------------------- */

function renderEvents(events) {
  for (const ev of events) {
    switch (ev.kind) {
      case "swing": {
        const attacker = ev.attacker === you.name ? null : actorByName(ev.attacker);
        const victim = actorByName(ev.victim) || actorById(ev.victim);
        if (ev.victim === you.name) updateHp(ev.victim_hp, ev.victim_max);
        if (victim) {
          victim.info.hp = ev.victim_hp;
          flash(victim, 0xcc2222);
          popup(victim, ev.crit ? `${ev.dmg}!` : String(ev.dmg),
                ev.crit ? "#f1c40f" : "#ffffff", ev.crit);
          if (targetId === String(victim.info.id) || targetId === ev.victim) updateTargetBar();
        }
        if (attacker) lunge(attacker, victim);
        logLine(ev.crit
          ? `${ev.attacker} CRITS ${ev.victim} for ${ev.dmg}!`
          : `${ev.attacker} hits ${ev.victim} for ${ev.dmg}.`, "hurt");
        break;
      }
      case "miss": {
        const victim = actorByName(ev.victim) || actorById(ev.victim);
        if (ev.victim === you.name) updateHp(ev.victim_hp, ev.victim_max);
        if (victim) popup(victim, "miss", "#8b95a3");
        logLine(`${ev.attacker} misses ${ev.victim}.`, "muted");
        break;
      }
      case "cast": {
        const target = actorByName(ev.target);
        if (ev.target === you.name) updateHp(ev.target_hp, ev.target_max);
        if (target) {
          target.info.hp = ev.target_hp;
          vigorBurst(target, ev.amount);
          if (healTargetName === ev.target) flash(target, 0x22aa55, 0.9);
        }
        logLine(`${ev.caster} casts vigor on ${ev.target} (+${ev.amount}).`, "heal");
        break;
      }
      case "die": {
        const dead = actorByName(ev.who);
        if (dead) {
          tween(0.6, (p) => {
            dead.group.rotation.z = (p * Math.PI) / 2.4;
            dead.group.position.y = -0.6 * p;
            dead.body.material.opacity = dead.head.material.opacity = 1 - p;
            dead.plate.material.opacity = 1 - p;
          }, () => {
            roomGroup.remove(dead.group);
            for (const [id, a] of actors) if (a === dead) actors.delete(id);
          });
        }
        if (ev.xp && ev.by === you.name) toast(`+${ev.xp} xp`);
        logLine(`${ev.who} dies${ev.by ? ` — slain by ${ev.by}` : ""}.`, ev.by === you.name ? "xp" : "muted");
        break;
      }
      case "spawn": {
        toast(`A ${ev.who.toLowerCase()} appears.`);
        logLine(`A ${ev.who} appears.`);
        break;
      }
      case "aggro": {
        if (ev.on === you.name) logLine(`The ${ev.who} turns on you!`, "hurt");
        break;
      }
    }
  }
}

/* ------------------------------ movement ------------------------------ */

async function goRoom(view) {
  const fade = $("#fade");
  fade.style.background = view.tint || "#000";
  fade.classList.add("on");
  targetId = null;
  fighting = false;
  $("#btn-attack").classList.remove("on");
  $("#btn-attack").textContent = "⚔ Attack";
  await sleep(300);
  buildRoom(view);
  camBob = 1.2; // a little walk-bob after the jump
  await sleep(70);
  fade.classList.remove("on");
  logLine(`You are in ${view.name}.`, "muted");
}

function updateCamera() {
  const x = Math.sin(camYaw) * CAM_R;
  const z = Math.cos(camYaw) * CAM_R;
  camera.position.set(x, CAM_H + camBob * 0.06, z);
  camera.lookAt(0, 1.0, 0);
}

/* ------------------------------- HUD ---------------------------------- */

function updateSelf() {
  if (!you) return;
  $("#char-name").textContent = `${you.name} · lvl ${you.level} · ${you.race} ${cap(you.class1)}`;
  updateHp(you.hp, you.max_hp);
  $("#xp-fill").style.width = `${Math.min(100, (you.xp / you.xp_next) * 100)}%`;
  const isCleric = you.class1 === "cleric" || you.class2 === "cleric";
  $("#btn-vigor").classList.toggle("hidden", !isCleric);
}

function updateHp(hp, max) {
  if (you) { you.hp = hp; you.max_hp = max; }
  const pct = max ? (hp / max) * 100 : 0;
  $("#hp-fill").style.width = `${pct}%`;
  $("#hp-text").textContent = `${hp} / ${max}`;
}

function updateTargetBar() {
  const bar = $("#target-bar");
  if (!targetId) { bar.classList.add("hidden"); return; }
  const a = actorById(targetId);
  if (!a || a.info.hp <= 0) { bar.classList.add("hidden"); targetId = null; return; }
  bar.classList.remove("hidden");
  $("#target-name").textContent = `${a.info.name} (lvl ${a.info.level})`;
  $("#target-fill").style.width = `${(a.info.hp / a.info.max_hp) * 100}%`;
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 3700);
}

function logLine(text, cls) {
  const log = $("#log");
  const el = document.createElement("div");
  if (cls) el.className = cls;
  el.textContent = text;
  log.append(el);
  while (log.childElementCount > 200) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : "";

/* --------------------------- input wiring ----------------------------- */

for (const btn of document.querySelectorAll("#dpad button")) {
  btn.onclick = () => {
    const dir = btn.dataset.dir;
    if (dir === "look") { send({ t: "look" }); return; }
    send({ t: "move", dir });
    camBob = 1.2;
  };
}

$("#btn-attack").onclick = () => {
  if (fighting) {
    fighting = false;
    send({ t: "attack", target: null });
    $("#btn-attack").classList.remove("on");
    $("#btn-attack").textContent = "⚔ Attack";
    return;
  }
  let id = targetId;
  if (!id || !actorById(id)) {
    const mob = [...actors.values()].find((a) => a.info.kind === "mob" && a.info.hp > 0);
    if (!mob) { toast("Nothing here to fight."); return; }
    id = String(mob.info.id);
  }
  targetId = id;
  fighting = true;
  send({ t: "attack", target: id });
  $("#btn-attack").classList.add("on");
  $("#btn-attack").textContent = `⚔ Stop`;
  updateTargetBar();
};

$("#btn-vigor").onclick = () => {
  send({ t: "vigor", target: healTargetName || you.name });
};

function bindSay() {
  const commit = () => {
    const text = $("#say-input").value.trim();
    if (text) { send({ t: "say", text }); $("#say-input").value = ""; }
  };
  $("#btn-say").onclick = commit;
  $("#say-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
  });
}
bindSay();

$("#btn-log").onclick = () => $("#log").classList.toggle("hidden");

/* pointer: drag = look around; tap = pick a target */
let dragStart = null;
canvas.addEventListener("pointerdown", (e) => {
  dragStart = { x: e.clientX, y: e.clientY, yaw: camYaw, t: performance.now() };
});
canvas.addEventListener("pointermove", (e) => {
  if (!dragStart) return;
  camYaw = dragStart.yaw - (e.clientX - dragStart.x) * 0.006;
});
canvas.addEventListener("pointerup", (e) => {
  if (!dragStart) return;
  const moved = Math.hypot(e.clientX - dragStart.x, e.clientY - dragStart.y);
  const quick = performance.now() - dragStart.t < 400;
  dragStart = null;
  if (moved < 12 && quick) pickTarget(e);
});

function pickTarget(e) {
  const ndc = new THREE.Vector2(
    (e.clientX / window.innerWidth) * 2 - 1,
    -(e.clientY / window.innerHeight) * 2 + 1);
  const ray = new THREE.Raycaster();
  ray.setFromCamera(ndc, camera);
  const groups = [...actors.values()].map((a) => a.group);
  const hits = ray.intersectObjects(groups, true);
  if (!hits.length) return;
  let obj = hits[0].object;
  while (obj && !actorsHasGroup(obj)) obj = obj.parent;
  if (!obj) return;
  for (const [id, a] of actors) {
    if (a.group === obj) {
      if (a.info.kind === "mob") {
        targetId = id;
        updateTargetBar();
        toast(`You eye the ${a.info.name}.`);
      } else {
        healTargetName = a.info.name;
        toast(`Vigor will target ${a.info.name}.`);
      }
      break;
    }
  }
}

function actorsHasGroup(obj) {
  for (const a of actors.values()) if (a.group === obj) return true;
  return false;
}

/* --------------------------- animation loop --------------------------- */

function animate(now) {
  requestAnimationFrame(animate);
  runTweens(now || performance.now());
  const t = (now || 0) / 1000;
  for (const { light, flame } of torches) {
    light.intensity = 10.5 + Math.sin(t * 9 + flame.parent.userData.phase) * 2.2;
    flame.scale.y = 1 + Math.sin(t * 13 + flame.parent.userData.phase) * 0.18;
  }
  for (const a of actors.values()) {
    const speed = a.group.userData.walking ? 10 : 2.6;
    const amp = a.group.userData.walking ? 0.06 : 0.025;
    a.group.position.y = Math.sin(t * speed + a.group.userData.phase) * amp;
    a.plate.material.opacity = 1;
  }
  if (camBob > 0) camBob = Math.max(0, camBob - 0.05);
  updateCamera();
  renderer.render(scene, camera);
}

/* ------------------------------- boot --------------------------------- */

function setView(name) {
  document.body.className = `view-${name}`;
}

function showGame() {
  setView("game");
  updateSelf();
  connect();
}

(async function boot() {
  if (!token) { setView("auth"); return; }
  try {
    const me = await api("/api/me");
    me.character ? (you = me.character, showGame()) : showCreate();
  } catch {
    localStorage.removeItem("r2m_token");
    token = null;
    setView("auth");
  }
})();

requestAnimationFrame(animate);
