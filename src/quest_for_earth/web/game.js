/* Quest for Earth — browser client.
 *
 * 2D top-down canvas view of each room (ships, bugs, loot, exits) and a
 * first-person target-shooter overlay for combat. The WebSocket stream
 * is the only source of truth (D2): the server spawns the target
 * squares, judges the shots and rolls the damage.
 */

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : "";

let token = localStorage.getItem("qfe_token") || null;
let you = null;
let roomView = null;
let ws = null;
let fighting = false;      // attack overlay open
let targetId = null;       // selected entity id for attack
let followFrom = null;     // pending follow request {from, from_id}
let loot = [];             // current room loot
let entities = [];         // rendered room entities with drift phases
let gameState = null;      // arena state: "freeform" (room-based) vs "arena" (grid)

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
    localStorage.setItem("qfe_token", token);
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

/* ======================== ship creation ========================== */

const ships = { classes: {}, types: {} };

async function showCreate() {
  setView("create");
  if (!ships.classes.bug) {
    Object.assign(ships, await api("/api/ships"));
    for (const [id, c] of Object.entries(ships.classes)) {
      $("#create-cls").add(new Option(`${c.name} — ${c.note}`, id));
    }
    for (const [id, t] of Object.entries(ships.types)) {
      $("#create-type").add(new Option(
        `${t.name}${t.min_level > 1 ? ` (lvl ${t.min_level}+)` : ""}`, id));
    }
    $("#create-cls").onchange = updateStatPreview;
    $("#create-type").onchange = updateStatPreview;
    updateStatPreview();
  }
}

function updateStatPreview() {
  const cls = ships.classes[$("#create-cls").value];
  const type = ships.types[$("#create-type").value];
  if (!cls || !type) return;
  const stats = { ...cls.stats };
  stats.agi = Math.max(1, Math.min(10, stats.agi + type.agi_mod));
  for (const key of ["str", "agi", "rep", "nrg", "trk"]) {
    $(`#stat-${key}`).textContent = stats[key];
  }
  $("#create-blurb").textContent = `${type.name}: ${type.note}`;
}

$("#create-submit").onclick = async () => {
  $("#create-error").textContent = "";
  try {
    await api("/api/character", {
      name: $("#create-name").value.trim(),
      cls: $("#create-cls").value,
      type: $("#create-type").value,
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
  ws.onclose = () => {
    if (document.body.classList.contains("view-game")) {
      toast("Connection lost — reconnecting…");
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
      msg.chart && renderChart(msg.chart);
      logLine(`Docked at ${msg.room.name}.`, "muted");
      break;
    case "room":
      goRoom(msg.room);
      break;
    case "enter":
      if (!you || msg.who.name !== you.name) {
        toast(`${msg.who.name} arrives.`);
        logLine(`${msg.who.name} arrives.`);
      }
      break;
    case "leave":
      logLine(`${msg.who} leaves.`, "muted");
      break;
    case "combat":
      renderEvents(msg.events);
      break;
    case "target":
      showTarget(msg);
      break;
    case "scan":
      toast(`Passive scan plotted: ${msg.body}`);
      logLine(`Scan plotted ${msg.body} — ${msg.room}.`, "muted");
      break;
    case "chart":
      renderChart(msg.entries);
      $("#chart-panel").classList.remove("hidden");
      break;
    case "loot":
      if (roomView && msg.room === roomView.id) {
        roomView.loot = msg.loot;
        rebuildEntities();
      }
      break;
    case "say":
      toast(`${msg.from}: ${msg.text}`);
      logLine(`${msg.from} says: ${msg.text}`);
      if (fighting) atkLog(`${msg.from}: ${msg.text}`);
      break;
    case "whisper":
      toast(`📓 ${msg.from} whispers: ${msg.text}`);
      logLine(`${msg.from} whispers: ${msg.text}`, "whisper");
      if (fighting) atkLog(`${msg.from} (whisper): ${msg.text}`);
      break;
    case "toast":
      toast(msg.text);
      logLine(msg.text, "muted");
      if (fighting) atkLog(msg.text);
      break;
    case "you":
      you = msg.you;
      updateSelf();
      break;
    case "follow_request":
      followFrom = msg;
      $("#follow-text").textContent =
        `${msg.from} asks to fly with you. Affiliated pilots share chat and follow each other into attacks.`;
      $("#follow-popup").classList.remove("hidden");
      break;
    case "error":
      toast(msg.text);
      if (msg.text.includes("create a character")) showCreate();
      break;
  }
}

/* ========================= 2D room renderer ========================= */

const canvas = $("#scene");
const ctx = canvas.getContext("2d");
const stars = Array.from({ length: 90 }, () => ({
  x: Math.random(), y: Math.random(), r: Math.random() * 1.4 + 0.3,
  p: Math.random() * Math.PI * 2, s: 0.4 + Math.random() * 1.2,
}));

function onResize() {
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  const ac = $("#attack-canvas");
  if (ac) { ac.width = window.innerWidth; ac.height = window.innerHeight; }
}
window.addEventListener("resize", onResize);
onResize();

let camZoom = 1;

function buildRoom(view) {
  roomView = view;
  loot = view.loot || [];
  rebuildEntities(view);
  const fade = $("#fade");
  fade.style.background = view.tint || "#000";
  fade.classList.add("on");
  setTimeout(() => fade.classList.remove("on"), 260);
  renderExits(view);
  logLine(`You are in ${view.name}.`, "muted");
}

function goRoom(view) {
  buildRoom(view);
  targetId = null;
  fighting = false;
  $("#attack").classList.add("hidden");
  $("#btn-attack").classList.remove("on");
  $("#btn-attack").textContent = "⚔ Attack";
}

function renderExits(view) {
  const box = $("#exits");
  box.innerHTML = "";
  for (const dir of Object.keys(view.exits || {})) {
    const tag = document.createElement("span");
    tag.className = "exit-tag";
    tag.textContent = `${dirArrow(dir)} ${cap(dir)}`;
    box.append(tag);
  }
}

const dirArrow = (d) => ({ north: "▲", south: "▼", east: "▶", west: "◀", up: "▲", down: "▼" }[d] || "•");

/* entity layout: ring positions around the room centre */
const SLOTS = [[0, -0.22], [0, 0.22], [-0.3, 0], [0.3, 0],
               [-0.22, -0.28], [0.22, -0.28], [-0.22, 0.28], [0.22, 0.28]];

function rebuildEntities(view = roomView) {
  entities = [];
  let i = 0;
  for (const p of view.players) {
    if (you && p.id === you.id) continue;
    entities.push({ info: p, slot: i++, type: "player" });
  }
  for (const m of view.mobs) {
    entities.push({ info: m, slot: i++, type: m.kind === "human" ? "npc" : "bug" });
  }
  for (const l of view.loot) {
    entities.push({ info: l, slot: i++, type: "loot" });
  }
}

function slotPos(i) {
  const s = SLOTS[i % SLOTS.length];
  return { x: 0.5 + s[0], y: 0.5 + s[1] };
}

function entityAt(px, py) {
  for (const e of entities) {
    const p = slotPos(e.slot);
    const sx = p.x * canvas.width, sy = p.y * canvas.height;
    if (Math.hypot(px - sx, py - sy) < 34) return e;
  }
  return null;
}

function drawEntity(ctx2, e, t) {
  const p = slotPos(e.slot);
  const x = p.x * canvas.width, y = p.y * canvas.height;
  const bob = Math.sin(t * 2 + e.slot * 1.7) * 3;
  ctx2.save();
  ctx2.translate(x, y + bob);

  if (e.type === "loot") {
    ctx2.rotate(Math.PI / 4);
    ctx2.fillStyle = "#e8b23a";
    ctx2.fillRect(-7, -7, 14, 14);
    ctx2.strokeStyle = "#fff8"; ctx2.strokeRect(-7, -7, 14, 14);
    ctx2.rotate(-Math.PI / 4);
    label(e.info.name, "#e8b23a", 26);
  } else if (e.type === "bug") {
    drawBug(ctx2, t + e.slot);
    label(`${e.info.name} L${e.info.level}`, "#f0808a", 30);
  } else if (e.type === "npc") {
    drawShip(ctx2, "#7fd4a8", t + e.slot);
    label(`${e.info.name}`, "#7fd4a8", 30);
  } else {
    const color = { bug: "#e0704a", human: "#4fa3ff", hybrid: "#9a7ff0" }[e.info.cls] || "#4fa3ff";
    drawShip(ctx2, color, t + e.slot);
    label(`${e.info.name} L${e.info.level}`, color, 30);
  }
  ctx2.restore();
}

function drawShip(c, color, t) {
  c.fillStyle = color;
  c.beginPath();
  c.moveTo(0, -16); c.lineTo(13, 12); c.lineTo(0, 6); c.lineTo(-13, 12);
  c.closePath(); c.fill();
  c.fillStyle = "#fff9";
  c.beginPath(); c.arc(0, -4, 3.2, 0, 7); c.fill();
  // engine glow
  c.fillStyle = `rgba(255,180,80,${0.4 + 0.3 * Math.sin(t * 6)})`;
  c.beginPath(); c.arc(0, 14, 4.5, 0, 7); c.fill();
}

function drawBug(c, t) {
  const wig = Math.sin(t * 5) * 3;
  c.fillStyle = "#7a9a3a";
  c.beginPath(); c.ellipse(0, 0, 18, 12, 0, 0, 7); c.fill();
  c.fillStyle = "#a4c45a";
  c.beginPath(); c.ellipse(0, -4, 10, 7, 0, 0, 7); c.fill();
  c.strokeStyle = "#5a7028"; c.lineWidth = 2.4;
  for (const s of [-1, 1]) for (let i = 0; i < 3; i++) {
    c.beginPath();
    c.moveTo(s * 10, -6 + i * 6);
    c.lineTo(s * (20 + wig), -10 + i * 9 + wig);
    c.stroke();
  }
  c.fillStyle = "#ff4a3a";
  c.beginPath(); c.arc(-4, -6, 1.8, 0, 7); c.arc(4, -6, 1.8, 0, 7); c.fill();
}

function label(text, color, y) {
  ctx2.font = "600 13px system-ui, sans-serif";
  ctx2.textAlign = "center";
  ctx2.shadowColor = "#000"; ctx2.shadowBlur = 6;
  ctx2.fillStyle = color;
  ctx2.fillText(text, 0, y + 16);
  ctx2.shadowBlur = 0;
}

function render(t) {
  const w = canvas.width, h = canvas.height;
  ctx.fillStyle = roomView ? (roomView.tint || "#05070d") : "#05070d";
  ctx.fillRect(0, 0, w, h);
  // starfield
  for (const s of stars) {
    ctx.fillStyle = `rgba(255,255,255,${0.25 + 0.5 * Math.abs(Math.sin(t * s + s.p))})`;
    ctx.fillRect(s.x * w, s.y * h, s.r, s.r);
  }
  // ring decorations for lanes / jump points
  if (roomView) {
    if (roomView.kind === "jump_point") {
      ctx.strokeStyle = "rgba(150,110,255,.35)"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.ellipse(w / 2, h / 2, 150, 46, 0.4, 0, 7); ctx.stroke();
    }
    // station/hull frame
    ctx.strokeStyle = "rgba(255,255,255,.06)";
    ctx.strokeRect(10, 10, w - 20, h - 20);
  }
  for (const e of entities) drawEntity(ctx, e, t / 1000);
  requestAnimationFrame(render);
}
requestAnimationFrame(render);

/* ========================= attack overlay ========================= */

const atkCanvas = $("#attack-canvas");
const atkCtx = atkCanvas.getContext("2d");
let foe = null;            // {name, id, hp, max_hp, mob_kind, level}
let liveTarget = null;     // {x, y, w, dwell, t0}
let foeDrift = 0;

function showTarget(msg) {
  liveTarget = { x: msg.x, y: msg.y, w: msg.w, dwell: msg.dwell, t0: performance.now() };
  window.__tgt = liveTarget;   // debug hook: makes the client testable
}

function enterAttack(victim) {
  foe = victim;
  fighting = true;
  $("#attack").classList.remove("hidden");
  $("#atk-foe-name").textContent = `${victim.name} (L${victim.level})`;
  $("#atk-me-name").textContent = `${you.name} — ${cap(you.cls)} ${cap(you.type)}`;
  atkLogClear();
  atkLog(`Engaging ${victim.name}.`);
  onResize();
}

function exitAttack() {
  fighting = false;
  liveTarget = null;
  $("#attack").classList.add("hidden");
}

function renderAttack(t) {
  const w = atkCanvas.width, h = atkCanvas.height;
  atkCtx.fillStyle = "#02030a";
  atkCtx.fillRect(0, 0, w, h);
  if (!foe) return;
  foeDrift = Math.sin(t / 900) * 14;
  const cx = w / 2 + foeDrift, cy = h * 0.52 + Math.cos(t / 1100) * 10;
  const R = Math.min(w, h) * 0.3;

  // the enemy: a large bug silhouette
  atkCtx.save();
  atkCtx.translate(cx, cy);
  atkCtx.fillStyle = "#4a2a1a";
  atkCtx.beginPath();
  atkCtx.ellipse(0, 0, R, R * 0.75, 0, 0, 7); atkCtx.fill();
  atkCtx.fillStyle = "#6b3a22";
  atkCtx.beginPath(); atkCtx.ellipse(0, -R * 0.2, R * 0.6, R * 0.42, 0, 0, 7); atkCtx.fill();
  atkCtx.strokeStyle = "#2a1810"; atkCtx.lineWidth = Math.max(4, R * 0.06);
  for (const s of [-1, 1]) for (let i = 0; i < 4; i++) {
    atkCtx.beginPath();
    atkCtx.moveTo(s * R * 0.7, -R * 0.4 + i * R * 0.26);
    atkCtx.lineTo(s * R * 1.25, -R * 0.6 + i * R * 0.36 + Math.sin(t / 300 + i) * 8);
    atkCtx.stroke();
  }
  atkCtx.fillStyle = "#ff4a3a";
  atkCtx.shadowColor = "#ff4a3a"; atkCtx.shadowBlur = 12;
  atkCtx.beginPath();
  atkCtx.arc(-R * 0.22, -R * 0.32, R * 0.05, 0, 7);
  atkCtx.arc(R * 0.22, -R * 0.32, R * 0.05, 0, 7);
  atkCtx.fill();
  atkCtx.shadowBlur = 0;
  atkCtx.restore();

  // the live target square (server-driven, fraction coords)
  if (liveTarget) {
    const age = (performance.now() - liveTarget.t0) / 1000;
    if (age > liveTarget.dwell) {
      liveTarget = null;
    } else {
      const drift = Math.sin(t / 700) * 6, drift2 = Math.cos(t / 500) * 6;
      const size = liveTarget.w * w;
      const sx = liveTarget.x * w + drift, sy = liveTarget.y * h + drift2;
      atkCtx.strokeStyle = `rgba(255,60,60,${0.9 - age / liveTarget.dwell * 0.4})`;
      atkCtx.lineWidth = 3;
      atkCtx.strokeRect(sx - size / 2, sy - size / 2, size, size);
      atkCtx.fillStyle = "rgba(255,60,60,.18)";
      atkCtx.fillRect(sx - size / 2, sy - size / 2, size, size);
    }
  }
  requestAnimationFrame(renderAttack);
}
requestAnimationFrame(renderAttack);

atkCanvas.addEventListener("click", (e) => {
  if (!fighting || !liveTarget) return;
  const x = e.clientX / atkCanvas.width;
  const y = e.clientY / atkCanvas.height;
  send({ t: "fire", x, y });
  liveTarget = null;   // consumed; server spawns the next one
});

$("#atk-evade").onclick = () => send({ t: "evade" });
$("#atk-repair").onclick = () => send({ t: "repair", target: "" });
$("#atk-break").onclick = () => {
  send({ t: "attack", target: "" });
  exitAttack();
};

function atkLog(text, cls) {
  const el = document.createElement("div");
  if (cls) el.className = cls;
  el.textContent = text;
  const box = $("#atk-log");
  box.append(el);
  while (box.childElementCount > 8) box.firstChild.remove();
}
const atkLogClear = () => { $("#atk-log").innerHTML = ""; };

/* ------------------------- combat events ------------------------- */

function renderEvents(events) {
  for (const ev of events) {
    switch (ev.kind) {
      case "swing": {
        const mine = you && (ev.attacker_id === you.id || ev.victim_id === you.id);
        if (ev.attacker_id === you.id || ev.attacker === you.name) {
          // my shot: show damage on the foe bar / overlay
          if (fighting && foe && (ev.victim === foe.name || ev.victim_id === foe.id)) {
            updateFoeHull(ev.victim_hp, ev.victim_max);
            atkLog(ev.crit ? `CRIT! ${ev.dmg} damage!` : `Hit for ${ev.dmg}.`,
                    ev.crit ? "xp" : "hurt");
          }
        } else if (ev.victim_id === you.id || ev.victim === you.name) {
          updateHull(ev.victim_hp, ev.victim_max);
          if (fighting) atkLog(`${ev.attacker} hits you for ${ev.dmg}.${ev.crit ? " CRIT!" : ""}`, "hurt");
          if (!fighting && foe == null && mine !== true) {
            // got attacked while cruising: open the overlay on the attacker
            enterAttack({ name: ev.attacker, id: ev.attacker_id, level: 1,
                          hp: 1, max_hp: 1, mob_kind: "bug" });
          }
        } else if (fighting) {
          atkLog(`${ev.attacker} hits ${ev.victim} for ${ev.dmg}.`, "hurt");
        } else {
          logLine(`${ev.attacker} hits ${ev.victim} for ${ev.dmg}.`, "hurt");
        }
        break;
      }
      case "repair": {
        const line = `${ev.tech} repairs ${ev.target} (+${ev.amount}).`;
        if (fighting) atkLog(line, "heal");
        logLine(line, "heal");
        if (you && ev.target === you.name) updateHull(ev.target_hp, ev.target_max);
        break;
      }
      case "die": {
        if (foe && (ev.who === foe.name)) {
          atkLog(`${ev.who} dies.${ev.xp ? ` +${ev.xp} xp` : ""}`, "xp");
          updateFoeHull(0, foe.max_hp);
          setTimeout(exitAttack, 900);
        } else {
          logLine(`${ev.who} dies.`, ev.by === (you && you.name) ? "xp" : "muted");
        }
        if (ev.xp && you && ev.by === you.name) toast(`+${ev.xp} xp`);
        break;
      }
      case "spawn":
        toast(`A ${ev.who} appears.`);
        logLine(`A ${ev.who} appears.`, "muted");
        break;
      case "aggro":
        if (ev.on === (you && you.name)) {
          logLine(`The ${ev.who} turns on you!`, "hurt");
          if (!fighting) {
            enterAttack({ name: ev.who, id: null, level: 1, hp: 1, max_hp: 1, mob_kind: "bug" });
          }
        }
        break;
    }
  }
}

/* ------------------------------- HUD ------------------------------- */

function updateSelf() {
  if (!you) return;
  $("#char-name").textContent =
    `${you.name} · L${you.level} · ${cap(you.cls)} ${cap(you.type)}`;
  updateHull(you.hp, you.max_hp);
  $("#nrg-fill").style.width = `${(you.energy / you.energy_max) * 100}%`;
  $("#nrg-text").textContent = `${you.energy} / ${you.energy_max} ⚡`;
  $("#xp-fill").style.width = `${Math.min(100, (you.xp / you.xp_next) * 100)}%`;
  $("#gear-guns").textContent = `🔫 ${you.guns.length}/${you.slots.guns}`;
  $("#gear-plates").textContent = `🛡 ${you.plates.length}/${you.slots.armor}`;
  $("#gear-artifacts").textContent = `🔮 ${you.artifacts.length}/${you.slots.artifacts}`;
}

function updateHull(hp, max) {
  if (you) { you.hp = hp; you.max_hp = max; }
  $("#hull-fill").style.width = `${max ? (hp / max) * 100 : 0}%`;
  $("#hull-text").textContent = `${hp} / ${max} 🛠`;
  $("#atk-hull-fill").style.width = `${max ? (hp / max) * 100 : 0}%`;
  $("#atk-hull-text").textContent = `${hp} / ${max}`;
  $("#atk-nrg-fill").style.width = you ? `${(you.energy / you.energy_max) * 100}%` : "0%";
  $("#atk-nrg-text").textContent = you ? `${you.energy} ⚡` : "";
}

function updateFoeHull(hp, max) {
  if (foe) { foe.hp = hp; foe.max_hp = max; }
  $("#atk-foe-fill").style.width = `${max ? (hp / max) * 100 : 0}%`;
  $("#atk-foe-text").textContent = `${hp} / ${max}`;
  if (targetBarMatches()) {
    $("#target-fill").style.width = `${max ? (hp / max) * 100 : 0}%`;
  }
}

function targetBarMatches() {
  const el = $("#target-name").textContent;
  return foe && el.startsWith(foe.name);
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

function renderChart(entries) {
  const list = $("#chart-list");
  list.innerHTML = "";
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "chart-entry" + (e.current ? " current" : "") + (e.permanent ? " perm" : "");
    const ttl = e.permanent ? "charted ✓" : `${Math.ceil((e.seconds || 0) / 60)}m`;
    row.innerHTML = `<span>${e.label}</span><span class="ttl">${ttl}</span>`;
    list.append(row);
  }
  $("#chart-panel").classList.remove("hidden");
}

/* --------------------------- input wiring ----------------------------- */

for (const btn of document.querySelectorAll("#dpad button")) {
  btn.onclick = () => {
    const dir = btn.dataset.dir;
    if (dir === "look") { send({ t: "look" }); return; }
    send({ t: "move", dir });
  };
}

$("#btn-attack").onclick = () => {
  if (fighting) { send({ t: "attack", target: "" }); exitAttack(); return; }
  if (targetId) { engage(targetId); return; }
  const bug = entities.find((e) => e.type === "bug");
  if (bug) { engage(bug.info.id); return; }
  const pl = entities.find((e) => e.type === "player");
  if (pl) { engage(pl.info.id); return; }
  toast("Nothing here to fight.");
};

function engage(id) {
  const e = entities.find((x) => String(x.info.id) === String(id));
  if (!e) return;
  targetId = String(id);
  send({ t: "attack", target: String(id) });
  enterAttack(e.info);
  $("#btn-attack").classList.add("on");
  $("#btn-attack").textContent = "⚔ Attacking";
}

$("#btn-evade").onclick = () => send({ t: "evade" });
$("#btn-repair").onclick = () => send({ t: "repair", target: "" });
$("#btn-talk").onclick = () => send({ t: "talk" });
$("#btn-chart").onclick = () => send({ t: "chart" });
$("#chart-close").onclick = () => $("#chart-panel").classList.add("hidden");
$("#follow-accept").onclick = () => {
  if (followFrom) send({ t: "accept", from: followFrom.from });
  $("#follow-popup").classList.add("hidden");
};
$("#follow-decline").onclick = () => {
  if (followFrom) send({ t: "decline" });
  $("#follow-popup").classList.add("hidden");
};

$("#btn-say").onclick = commitSay;
$("#say-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); commitSay(); }
});
function commitSay() {
  const text = $("#say-input").value.trim();
  if (!text) return;
  if (text.startsWith("/w ")) {
    const rest = text.slice(3);
    const target = rest.split(" ")[0];
    send({ t: "whisper", target, text: rest.slice(target.length + 1) });
  } else if (text.startsWith("/follow ")) {
    send({ t: "follow", target: text.slice(8).trim() });
  } else if (text.startsWith("/jump ")) {
    send({ t: "jump", destination: text.slice(6).trim() });
  } else if (text.startsWith("/take ")) {
    send({ t: "take", item: text.slice(6).trim() });
  } else {
    send({ t: "say", text });
  }
  $("#say-input").value = "";
}

$("#btn-log").onclick = () => $("#log").classList.toggle("hidden");

/* click entities in the room: attack bugs / follow players / take loot */
canvas.addEventListener("click", (e) => {
  const hit = entityAt(e.clientX, e.clientY);
  if (!hit) return;
  if (hit.type === "loot") {
    send({ t: "take", item: hit.info.item });
  } else if (hit.type === "bug") {
    engage(hit.info.id);
  } else if (hit.type === "npc") {
    toast("Click 💬 Talk to chat with them.");
  } else {
    send({ t: "follow", target: hit.info.name });
  }
});

/* --------------------------- animation loop --------------------------- */

function animate(now) {
  render(now);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

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
    localStorage.removeItem("qfe_token");
    token = null;
    setView("auth");
  }
})();
