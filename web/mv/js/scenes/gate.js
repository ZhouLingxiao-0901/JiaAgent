import { PAL } from "../engine/palette.js";
import { drawText } from "../engine/pixeltext.js";
import { blit, clamp, easeInOut, envelope, mulberry32 } from "../engine/util.js";

const W = 640;
const H = 360;

function makeMotes() {
  const rnd = mulberry32(0x91a3);
  return Array.from({ length: 42 }, () => ({
    x: rnd() * 760,
    y: 40 + rnd() * 270,
    speed: 2 + rnd() * 5,
    sway: 2 + rnd() * 5,
    phase: rnd() * Math.PI * 2,
    size: rnd() < 0.25 ? 2 : 1,
    warm: rnd() < 0.7,
    alpha: 0.18 + rnd() * 0.38,
  }));
}

function makeLeaves() {
  const rnd = mulberry32(0x5c1d);
  return Array.from({ length: 7 }, () => ({
    x: rnd() * 760,
    y: 220 + rnd() * 110,
    speed: 4 + rnd() * 7,
    phase: rnd() * Math.PI * 2,
    color: rnd() < 0.55 ? PAL.warmMid : PAL.redSoft,
  }));
}

export function createGateScene(assets) {
  const motes = makeMotes();
  const leaves = makeLeaves();

  return {
    id: "gate",
    draw(ctx, t) {
      const p = clamp(t / 8.5);
      const camX = 58 - easeInOut(p) * 38; // 58 -> 20, a very slow drift
      const titleAlpha = envelope(t, 0.9, 2.1, 6.1, 7.2);

      ctx.save();
      ctx.imageSmoothingEnabled = false;

      // Background and a slightly faster foreground layer: 1.25 px of
      // parallax at the end of the shot.
      ctx.drawImage(assets.bg.gate, -Math.round(camX), 0);
      ctx.drawImage(assets.bg.gateFg, -Math.round(camX * 1.25), 0);

      // The sun sits at (626, 132) in the source image.  A soft warm halo
      // breathes slowly; it is drawn before the particles so they sparkle.
      const sunX = 626 - camX;
      const pulse = 0.16 + Math.sin(t * 1.1) * 0.025;
      blit(ctx, assets.fx.glowWarm, sunX, 132, 176, 176, pulse);
      blit(ctx, assets.fx.glowWarm, sunX, 132, 96, 96, 0.2);

      // Dust in the low sunlight.
      for (const m of motes) {
        const x = ((m.x - camX * 0.6 + Math.sin(t * 0.5 + m.phase) * m.sway) % 760 + 760) % 760;
        const y = ((m.y - t * m.speed) % 360 + 360) % 360;
        if (x < -4 || x > W + 4 || y < -4 || y > H) continue;
        const twinkle = 0.45 + 0.55 * Math.sin(t * 1.4 + m.phase);
        ctx.globalAlpha = m.alpha * twinkle;
        ctx.fillStyle = m.warm ? PAL.warm : PAL.cream;
        ctx.fillRect(Math.round(x), Math.round(y), m.size, 1);
      }
      ctx.globalAlpha = 1;

      // A few leaves skittering along the pavement.
      for (const l of leaves) {
        const x = ((l.x + t * l.speed) % 780) - camX;
        const y = l.y + Math.sin(t * 0.9 + l.phase) * 3;
        if (x < -3 || x > W + 3) continue;
        ctx.globalAlpha = 0.55;
        ctx.fillStyle = l.color;
        ctx.fillRect(Math.round(x), Math.round(y), 2, 1);
        ctx.fillRect(Math.round(x) + 1, Math.round(y) + 1, 1, 1);
      }
      ctx.globalAlpha = 1;

      // Title card.  Placed in the open sky so it never fights the gate sign.
      if (titleAlpha > 0.002) {
        ctx.globalAlpha = titleAlpha;
        drawText(ctx, "刻在我心底的名字", 320, 25, {
          size: 20,
          color: PAL.cream,
          align: "center",
          letterSpacing: 2,
          bold: true,
          shadow: true,
          shadowColor: PAL.ink,
        });

        const subAlpha = envelope(t, 1.8, 2.8, 6.0, 7.0);
        if (subAlpha > 0) {
          ctx.globalAlpha = subAlpha * 0.9;
          const w = drawText(ctx, "样片 01", 320, 54, {
            size: 9,
            color: PAL.warm,
            align: "center",
            letterSpacing: 4,
            shadow: true,
            shadowColor: PAL.ink,
          });
          ctx.globalAlpha = subAlpha * 0.42;
          ctx.fillStyle = PAL.warm;
          ctx.fillRect(320 - w / 2 - 28, 58, 20, 1);
          ctx.fillRect(320 + w / 2 + 8, 58, 20, 1);
        }
        ctx.globalAlpha = 1;
      }

      // Small chapter stamp.  Deliberately low contrast.
      const stamp = envelope(t, 2.2, 3.2, 6.4, 7.4);
      if (stamp > 0) {
        ctx.globalAlpha = stamp * 0.62;
        drawText(ctx, "高一 · 秋", 608, 332, {
          size: 9,
          color: PAL.cream,
          align: "right",
          letterSpacing: 1,
          shadow: true,
          shadowColor: PAL.ink2,
        });
        ctx.globalAlpha = 1;
      }

      ctx.restore();
    },
  };
}
