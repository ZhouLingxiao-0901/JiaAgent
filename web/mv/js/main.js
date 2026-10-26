import { MVClock } from "./engine/clock.js";
import { SpriteBank } from "./engine/sprites.js";
import { drawText } from "./engine/pixeltext.js";
import { PAL } from "./engine/palette.js";
import { smoothstep } from "./engine/util.js";
import { createGateScene } from "./scenes/gate.js";
import { createClassroomScene } from "./scenes/classroom.js";
import { createBedroomScene } from "./scenes/bedroom.js";

const W = 640;
const H = 360;
const DURATION = 40;

const $ = (id) => document.getElementById(id);

const canvas = $("screen");
const ctx = canvas.getContext("2d", { alpha: false });
ctx.imageSmoothingEnabled = false;

const stage = $("stage");
const loading = $("loading");
const playBtn = $("play");
const replayBtn = $("replay");
const fsBtn = $("fs");
const debugBtn = $("debug-btn");
const debugPanel = $("debug");
const scrub = $("scrub");
const timeNow = $("time-now");

const dTime = $("d-time");
const dScene = $("d-scene");
const dLocal = $("d-local");
const dFrame = $("d-frame");
const dFps = $("d-fps");
const dScale = $("d-scale");

// Two full-scene buffers are enough: at most two scenes overlap during a
// crossfade.  Rendering each scene into a buffer isolates its own alpha work
// from the crossfade compositing.
const buffers = [document.createElement("canvas"), document.createElement("canvas")];
const bufCtxs = buffers.map((b) => {
  b.width = W;
  b.height = H;
  const c = b.getContext("2d", { alpha: false });
  c.imageSmoothingEnabled = false;
  return c;
});

const clock = new MVClock(DURATION, { autoplay: false });
let scenes = [];
let debugOn = false;
let scaleDev = 1;
let cssScale = 1;
let fps = 0;
let lastFrame = performance.now();
let fpsAccum = 0;
let fpsFrames = 0;
let lastUiAt = 0;
let booted = false;

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const done = () => resolve(img);
      if (img.decode) img.decode().then(done).catch(done);
      else done();
    };
    img.onerror = () => reject(new Error(`无法载入 ${src}`));
    img.src = src;
  });
}

async function loadAssets() {
  const paths = {
    gate: "assets/bg/gate_dusk.png",
    gateFg: "assets/bg/gate_dusk_fg.png",
    classroom: "assets/bg/classroom_day.png",
    classroomFg: "assets/bg/classroom_day_fg.png",
    bedroomMale: "assets/bg/bedroom_male_night.png",
    bedroomFemale: "assets/bg/bedroom_female_night.png",
    bedroomMaleFg: "assets/bg/bedroom_male_fg.png",
    bedroomFemaleFg: "assets/bg/bedroom_female_fg.png",
    male: "assets/sprites/male.png",
    female: "assets/sprites/female.png",
    glowBlue: "assets/fx/glow_blue.png",
    glowWarm: "assets/fx/glow_warm.png",
    glowMoon: "assets/fx/glow_moon.png",
  };

  const entries = await Promise.all(
    Object.entries(paths).map(async ([key, src]) => [key, await loadImage(src)])
  );
  const imgs = Object.fromEntries(entries);

  const res = await fetch("assets/sprites/manifest.json", { cache: "no-store" });
  if (!res.ok) throw new Error(`无法载入精灵表清单（HTTP ${res.status}）`);
  const manifest = await res.json();

  return {
    bg: {
      gate: imgs.gate,
      gateFg: imgs.gateFg,
      classroom: imgs.classroom,
      classroomFg: imgs.classroomFg,
      bedroomMale: imgs.bedroomMale,
      bedroomFemale: imgs.bedroomFemale,
      bedroomMaleFg: imgs.bedroomMaleFg,
      bedroomFemaleFg: imgs.bedroomFemaleFg,
    },
    fx: {
      glowBlue: imgs.glowBlue,
      glowWarm: imgs.glowWarm,
      glowMoon: imgs.glowMoon,
    },
    sprites: new SpriteBank({ male: imgs.male, female: imgs.female }, manifest),
  };
}

const TRANSITIONS = [
  { a: 7.5, b: 8.5, from: "gate", to: "classroom" },
  { a: 23.5, b: 24.5, from: "classroom", to: "bedroom" },
];

function buildScenes(assets) {
  const gate = createGateScene(assets);
  const classroom = createClassroomScene(assets);
  const bedroom = createBedroomScene(assets);

  return [
    { id: "gate", label: "黄昏校园", start: 0, end: 8.5, scene: gate },
    { id: "classroom", label: "教室初识", start: 7.5, end: 24.5, scene: classroom },
    { id: "bedroom", label: "夜晚聊天", start: 23.5, end: 40, scene: bedroom },
  ];
}

function sceneForTime(t) {
  for (const tr of TRANSITIONS) {
    if (t >= tr.a && t < tr.b) {
      const id = t < (tr.a + tr.b) / 2 ? tr.from : tr.to;
      return scenes.find((s) => s.id === id) || null;
    }
  }
  let active = null;
  for (const s of scenes) {
    if (t >= s.start && t <= s.end) active = s;
  }
  return active;
}

function transitionBlackAt(t) {
  for (const tr of TRANSITIONS) {
    if (t < tr.a || t >= tr.b) continue;
    const mid = (tr.a + tr.b) / 2;
    if (t < mid) return smoothstep((t - tr.a) / (mid - tr.a));
    return 1 - smoothstep((t - mid) / (tr.b - mid));
  }
  return 0;
}

function drawScene(s, local) {
  const bctx = bufCtxs[0];
  bctx.setTransform(1, 0, 0, 1, 0, 0);
  bctx.globalAlpha = 1;
  bctx.globalCompositeOperation = "source-over";
  bctx.fillStyle = "#000";
  bctx.fillRect(0, 0, W, H);
  s.scene.draw(bctx, local);
  ctx.globalAlpha = 1;
  ctx.drawImage(buffers[0], 0, 0);
}

function drawAt(t) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = "source-over";
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, W, H);

  // Scenes are joined by a 1 s dip to black (0.5 s out, 0.5 s in).  Switching
  // pictures at the fully black midpoint avoids the double-exposure look of a
  // straight cross-dissolve, and reads as a blink between memories.
  const s = sceneForTime(t);
  if (s) drawScene(s, Math.max(0, t - s.start));

  let black = transitionBlackAt(t);
  if (t < 1.0) black = Math.max(black, 1 - smoothstep(t / 1.0));
  if (t > DURATION - 1.5) {
    black = Math.max(black, smoothstep((t - (DURATION - 1.5)) / 1.5));
  }
  if (black > 0.001) {
    ctx.globalAlpha = black;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);
    ctx.globalAlpha = 1;
  }

  if (debugOn) drawDebugOverlay(t);
}

function drawDebugOverlay(t) {
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha = 0.55;
  ctx.fillStyle = PAL.warm;
  const inset = 24;
  // 1 px safe-area border
  ctx.fillRect(inset, inset, W - inset * 2, 1);
  ctx.fillRect(inset, H - inset - 1, W - inset * 2, 1);
  ctx.fillRect(inset, inset, 1, H - inset * 2);
  ctx.fillRect(W - inset - 1, inset, 1, H - inset * 2);
  // centre cross
  ctx.globalAlpha = 0.32;
  ctx.fillRect(W / 2 - 6, H / 2, 13, 1);
  ctx.fillRect(W / 2, H / 2 - 6, 1, 13);
  ctx.globalAlpha = 0.65;
  drawText(ctx, "SAFE 24", inset + 3, inset + 3, {
    size: 8,
    color: PAL.warm,
    threshold: 80,
  });
  drawText(ctx, `${t.toFixed(2)}s`, inset + 3, H - inset - 14, {
    size: 8,
    color: PAL.warm,
    threshold: 80,
  });
  ctx.restore();
}

function fmt(t) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}

function updateUI(t, now) {
  const active = sceneForTime(t);
  if (now - lastUiAt < 70) return;
  lastUiAt = now;

  timeNow.textContent = fmt(t);
  scrub.value = String(t);

  if (clock.ended) playBtn.textContent = "重播";
  else if (clock.playing) playBtn.textContent = "暂停";
  else playBtn.textContent = "播放";

  if (debugOn) {
    dTime.textContent = `${t.toFixed(3)}s`;
    dScene.textContent = active ? active.id : "—";
    dLocal.textContent = active ? `${Math.max(0, t - active.start).toFixed(3)}s` : "—";
    dFrame.textContent = String(Math.floor(t * 60));
    dFps.textContent = fps > 0 ? fps.toFixed(0) : "—";
    dScale.textContent = `${scaleDev}× dev · ${Math.round(W * cssScale)}×${Math.round(H * cssScale)} css`;
  }
}

function loop(now) {
  const dt = now - lastFrame;
  lastFrame = now;
  if (dt > 0 && dt < 500) {
    fpsAccum += dt;
    fpsFrames++;
    if (fpsAccum >= 500) {
      fps = (fpsFrames * 1000) / fpsAccum;
      fpsAccum = 0;
      fpsFrames = 0;
    }
  }

  const t = clock.time();
  drawAt(t);
  updateUI(t, now);
  requestAnimationFrame(loop);
}

function resize() {
  const rect = stage.getBoundingClientRect();
  const availW = Math.max(1, rect.width - 10);
  const availH = Math.max(1, rect.height - 10);
  const dpr = window.devicePixelRatio || 1;

  const maxDev = Math.min((availW * dpr) / W, (availH * dpr) / H);
  scaleDev = Math.floor(maxDev);
  if (scaleDev < 1) scaleDev = Math.max(0.5, maxDev); // tiny-window fallback

  cssScale = scaleDev / dpr;
  canvas.style.width = `${W * cssScale}px`;
  canvas.style.height = `${H * cssScale}px`;

  if (debugOn) {
    dScale.textContent = `${scaleDev}× dev · ${Math.round(W * cssScale)}×${Math.round(H * cssScale)} css`;
  }
}

function setDebug(on) {
  debugOn = Boolean(on);
  debugPanel.hidden = !debugOn;
  debugBtn.setAttribute("aria-pressed", String(debugOn));
  requestAnimationFrame(resize);
}

function togglePlay() {
  if (clock.ended) {
    clock.reset();
    clock.play();
  } else {
    clock.toggle();
  }
}

function replay() {
  clock.reset();
  clock.play();
}

function seekBy(delta) {
  clock.seek(clock.time() + delta);
}

async function boot() {
  try {
    const assets = await loadAssets();
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready.catch(() => {});
    }
    scenes = buildScenes(assets);
    booted = true;

    resize();
    drawAt(0);
    updateUI(0, performance.now());
    loading.hidden = true;
    clock.play();
    lastFrame = performance.now();
    requestAnimationFrame(loop);

    window.__MV__ = {
      clock,
      seek(t) {
        clock.seek(t);
        drawAt(clock.time());
        updateUI(clock.time(), performance.now());
      },
      play: () => clock.play(),
      pause: () => clock.pause(),
      toggle: togglePlay,
      debug: setDebug,
      get time() { return clock.time(); },
      get ready() { return booted; },
      get scenes() { return scenes.map((s) => ({ id: s.id, start: s.start, end: s.end })); },
      assets,
    };
    console.info("[MV] pixel sample ready — window.__MV__ available for timeline debugging");
  } catch (err) {
    console.error(err);
    loading.classList.add("error");
    loading.textContent = `素材载入失败：${err.message}\n请确认通过本地服务器访问 /mv/`;
  }
}

playBtn.addEventListener("click", () => {
  togglePlay();
  playBtn.blur();
});
replayBtn.addEventListener("click", () => {
  replay();
  replayBtn.blur();
});
fsBtn.addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) await stage.requestFullscreen();
    else await document.exitFullscreen();
  } catch (err) {
    console.warn("Fullscreen unavailable", err);
  }
  fsBtn.blur();
});
debugBtn.addEventListener("click", () => {
  setDebug(!debugOn);
  debugBtn.blur();
});
scrub.addEventListener("input", () => {
  clock.seek(parseFloat(scrub.value));
});
scrub.addEventListener("change", () => {
  clock.seek(parseFloat(scrub.value));
});

document.addEventListener("fullscreenchange", () => {
  fsBtn.textContent = document.fullscreenElement ? "退出全屏" : "全屏";
  setTimeout(resize, 60);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clock.pause();
  }
  lastFrame = performance.now();
});

window.addEventListener("keydown", (e) => {
  if (!booted) return;
  const tag = e.target?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (tag === "BUTTON" && (e.code === "Space" || e.code === "Enter")) return;

  switch (e.code) {
    case "Space":
      e.preventDefault();
      togglePlay();
      break;
    case "ArrowLeft":
      e.preventDefault();
      seekBy(e.shiftKey ? -0.1 : -1);
      break;
    case "ArrowRight":
      e.preventDefault();
      seekBy(e.shiftKey ? 0.1 : 1);
      break;
    case "Home":
      e.preventDefault();
      clock.seek(0);
      break;
    case "End":
      e.preventDefault();
      clock.seek(DURATION);
      break;
    case "KeyR":
      e.preventDefault();
      replay();
      break;
    case "KeyD":
      e.preventDefault();
      setDebug(!debugOn);
      break;
    default:
      break;
  }
});

window.addEventListener("resize", resize);
if ("ResizeObserver" in window) {
  new ResizeObserver(() => resize()).observe(stage);
}

resize();
boot();
