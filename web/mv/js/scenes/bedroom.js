import { PAL } from "../engine/palette.js";
import { drawText, measureText } from "../engine/pixeltext.js";
import { blit, clamp, envelope, mulberry32, smoothstep } from "../engine/util.js";

const W = 640;
const H = 360;
const SPLIT = 319;
const FLOOR_Y = 324;

const PX = 230;
const PY = 166;
const PW = 180;
const PH = 192;
const MSG_STEP = 20;
const BUBBLE_H = 19;
const MSG_BOTTOM = PY + PH - 4 - BUBBLE_H;
const FG_Y = 8;
const TYPE_SPEED = 0.5; // seconds per message
const SCROLL_T = 0.22; // a new message nudges the stack up over this long

const MESSAGES = [
  { t: 1.6, who: "him", text: "在干嘛？" },
  { t: 3.0, who: "her", text: "写作业。" },
  { t: 4.2, who: "her", text: "好无聊。" },
  { t: 5.4, who: "him", text: "我也是。" },
  { t: 6.8, who: "him", text: "外面下雨了。" },
  { t: 8.4, who: "her", text: "嗯，我这边也是。" },
  { t: 9.8, who: "him", text: "早点睡。" },
  { t: 11.1, who: "her", text: "晚安。" },
];

function makeRain() {
  const rnd = mulberry32(0x71c3);
  const make = (n) => Array.from({ length: n }, () => ({
    x: rnd(),
    y: rnd(),
    speed: 24 + rnd() * 26,
    len: 3 + Math.floor(rnd() * 3),
    alpha: 0.12 + rnd() * 0.22,
  }));
  return { male: make(26), female: make(24) };
}

function drawRain(ctx, drops, rx, ry, rw, rh, t) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(rx, ry, rw, rh);
  ctx.clip();
  ctx.fillStyle = PAL.cool;
  for (const d of drops) {
    const x = rx + ((d.x * rw + t * 5) % rw);
    const y = ry + ((d.y * rh + t * d.speed) % rh);
    ctx.globalAlpha = d.alpha;
    ctx.fillRect(Math.round(x), Math.round(y), 1, d.len);
    ctx.fillRect(Math.round(x) - 1, Math.round(y) + d.len, 1, 1);
  }
  ctx.restore();
}

function phoneFrame(t, phase, blinking) {
  if (blinking) return `phone_front_${2 + (Math.floor(t * 14) % 2)}`;
  return `phone_front_${Math.floor(t * 3 + phase) % 2}`;
}

export function createBedroomScene(assets) {
  const rain = makeRain();

  return {
    id: "bedroom",
    draw(ctx, t) {
      const panelAlpha = envelope(t, 0.7, 1.4, 12.6, 13.4);
      const glowAlpha = 1 - envelope(t, 12.4, 12.9, 13.4, 13.9);
      const phonesDown = t >= 12.9;
      const caption = envelope(t, 0.9, 1.9, 3.5, 4.5);
      const moon = envelope(t, 12.7, 14.1, 99, 100);

      ctx.save();
      ctx.imageSmoothingEnabled = false;

      // --- Two rooms -----------------------------------------------------
      ctx.drawImage(assets.bg.bedroomMale, 0, 0);
      ctx.drawImage(assets.bg.bedroomFemale, 320, 0);

      drawRain(ctx, rain.male, 194, 40, 102, 120, t);
      drawRain(ctx, rain.female, 340, 44, 104, 120, t);

      // Lamps and the cold window light.
      blit(ctx, assets.fx.glowWarm, 300, 182, 52, 52, 0.1);
      blit(ctx, assets.fx.glowWarm, 430, 184, 52, 52, 0.09);

      // --- Characters ----------------------------------------------------
      const blinkM = (t % 3.1) > 2.94;
      const blinkF = ((t + 1.2) % 3.7) > 3.53;

      let maleFrame;
      let femaleFrame;
      if (phonesDown) {
        maleFrame = `look_front_${Math.floor(t * 2) % 2}`;
        femaleFrame = `look_front_${Math.floor(t * 2 + 1) % 2}`;
      } else {
        maleFrame = phoneFrame(t, 0, blinkM);
        femaleFrame = phoneFrame(t, 1, blinkF);
      }

      assets.sprites.draw(ctx, "male", maleFrame, 140, FLOOR_Y);
      assets.sprites.draw(ctx, "female", femaleFrame, 520, FLOOR_Y);

      // Phone light on faces and hands.
      if (glowAlpha > 0) {
        ctx.save();
        ctx.globalCompositeOperation = "screen";
        blit(ctx, assets.fx.glowBlue, 140, 292, 58, 58, 0.34 * glowAlpha);
        blit(ctx, assets.fx.glowBlue, 520, 292, 58, 58, 0.34 * glowAlpha);
        ctx.restore();
      }

      // Blanket foreground hides the lower body and sells "in bed".
      ctx.drawImage(assets.bg.bedroomMaleFg, 0, FG_Y);
      ctx.drawImage(assets.bg.bedroomFemaleFg, 320, FG_Y);

      // --- Same moon -----------------------------------------------------
      if (moon > 0) {
        const breathe = 0.75 + Math.sin(t * 0.8) * 0.25;
        ctx.save();
        ctx.globalCompositeOperation = "screen";
        blit(ctx, assets.fx.glowMoon, 250, 82, 78, 78, 0.3 * moon * breathe);
        blit(ctx, assets.fx.glowMoon, 390, 86, 78, 78, 0.3 * moon * breathe);
        blit(ctx, assets.fx.glowMoon, 250, 82, 150, 150, 0.08 * moon);
        blit(ctx, assets.fx.glowMoon, 390, 86, 150, 150, 0.08 * moon);
        ctx.restore();
      }

      // Divider between the two rooms.
      ctx.fillStyle = "#111624";
      ctx.fillRect(SPLIT, 0, 2, H);
      ctx.globalAlpha = 0.22;
      ctx.fillStyle = PAL.coolMid;
      ctx.fillRect(SPLIT + 2, 0, 1, H);
      ctx.globalAlpha = 1;

      if (caption > 0) {
        ctx.globalAlpha = caption * 0.62;
        drawText(ctx, "疫情期间 · 各自在家", 320, 8, {
          size: 9,
          color: PAL.cream,
          align: "center",
          letterSpacing: 1,
          shadow: true,
          shadowColor: PAL.ink,
        });
        ctx.globalAlpha = 1;
      }

      // --- Chat overlay ---------------------------------------------------
      if (panelAlpha > 0) {
        ctx.save();
        ctx.globalAlpha = panelAlpha;
        drawChat(ctx, t);
        ctx.restore();
      }

      ctx.restore();
    },
  };
}

function drawChat(ctx, t) {
  // Panel shadow and body.
  ctx.fillStyle = "rgba(0,0,0,0.42)";
  ctx.fillRect(PX + 3, PY + 3, PW, PH);
  ctx.fillStyle = "#0e1526";
  ctx.fillRect(PX, PY, PW, PH);
  ctx.fillStyle = "#3a4a68";
  ctx.fillRect(PX, PY, PW, 1);
  ctx.fillRect(PX, PY + PH - 1, PW, 1);
  ctx.fillRect(PX, PY, 1, PH);
  ctx.fillRect(PX + PW - 1, PY, 1, PH);
  ctx.fillStyle = "#22304b";
  ctx.fillRect(PX + 1, PY + 1, PW - 2, 1);

  // Header.
  ctx.fillStyle = "#4f8f6a";
  ctx.fillRect(PX + 8, PY + 10, 3, 3);
  drawText(ctx, "聊天", PX + 15, PY + 4, {
    size: 13,
    color: PAL.cream,
    shadow: true,
    shadowColor: PAL.ink,
    threshold: 96,
  });
  drawText(ctx, "22:47", PX + PW - 8, PY + 7, {
    size: 8,
    color: PAL.whiteDk,
    align: "right",
    threshold: 80,
  });
  ctx.save();
  ctx.globalAlpha *= 0.5;
  ctx.fillStyle = "#3a4a68";
  ctx.fillRect(PX + 7, PY + 22, PW - 14, 1);
  ctx.restore();

  // Show a typing bubble for the next message, then the messages themselves.
  let typing = null;
  for (let i = 0; i < MESSAGES.length; i++) {
    const m = MESSAGES[i];
    if (t < m.t && m.t - t < 0.42) typing = i;
  }
  if (typing !== null) drawTyping(ctx, MESSAGES[typing], typing, t);

  for (let i = 0; i < MESSAGES.length; i++) {
    const m = MESSAGES[i];
    if (t < m.t) break;
    drawMessage(ctx, m, i, t);
  }

}

// Messages sit at the bottom of the panel like a real chat window.  Each new
// message nudges the earlier stack up by one row, animated over SCROLL_T so
// the panel never grows downwards from an empty top edge.
function bubbleY(index, t) {
  let y = MSG_BOTTOM;
  for (let j = index + 1; j < MESSAGES.length; j++) {
    const mt = MESSAGES[j].t;
    if (t >= mt) y -= smoothstep(clamp((t - mt) / SCROLL_T)) * MSG_STEP;
  }
  return Math.round(y);
}

function bubbleBox(m, index, t) {
  const tw = measureText(m.text, 13);
  const bw = Math.min(PW - 12, tw + 12);
  const bx = m.who === "him" ? PX + 6 : PX + PW - 6 - bw;
  const by = bubbleY(index, t);
  return { bx, by, bw, tw };
}

function drawMessage(ctx, m, index, t) {
  const { bx, by, bw, tw } = bubbleBox(m, index, t);
  const rev = clamp((t - m.t) / TYPE_SPEED);

  ctx.fillStyle = m.who === "him" ? PAL.navyDk : PAL.plum;
  ctx.fillRect(bx, by, bw, BUBBLE_H);
  ctx.fillStyle = m.who === "him" ? PAL.navy : PAL.rose;
  ctx.fillRect(bx, by, bw, 1);

  drawText(ctx, m.text, bx + 6, by + 3, {
    size: 13,
    color: PAL.cream,
    reveal: rev,
    threshold: 96,
  });

  if (rev < 1) {
    const blink = Math.floor(t * 10) % 2 === 0;
    if (blink) {
      ctx.fillStyle = PAL.warm;
      ctx.fillRect(bx + 6 + Math.round(tw * rev), by + 9, 3, 1);
    }
  }
}

function drawTyping(ctx, m, index, t) {
  const { bx, by } = bubbleBox(m, index, t);
  const base = ctx.globalAlpha;
  const w = 26;
  const x = m.who === "him" ? bx : PX + PW - 6 - w;
  ctx.save();
  ctx.globalAlpha = base;
  ctx.fillStyle = m.who === "him" ? PAL.navyDk : PAL.plum;
  ctx.fillRect(x, by, w, 15);
  const phase = Math.floor(t * 6) % 3;
  ctx.fillStyle = PAL.whiteSh;
  for (let i = 0; i < 3; i++) {
    ctx.globalAlpha = base * (i === phase ? 1 : 0.45);
    ctx.fillRect(x + 8 + i * 4, by + 6, 2, 2);
  }
  ctx.restore();
}
