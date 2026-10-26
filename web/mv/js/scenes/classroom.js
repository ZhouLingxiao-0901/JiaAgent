import { PAL } from "../engine/palette.js";
import { drawText } from "../engine/pixeltext.js";
import { blit, clamp, easeInOut, envelope, lerp, mulberry32, smoothstep } from "../engine/util.js";

const W = 640;
const H = 360;

function makeDust() {
  const rnd = mulberry32(0x2f6b);
  return Array.from({ length: 34 }, () => ({
    x: 330 + rnd() * 340,
    y: 70 + rnd() * 210,
    speed: 1.5 + rnd() * 3,
    sway: 2 + rnd() * 4,
    phase: rnd() * Math.PI * 2,
    alpha: 0.12 + rnd() * 0.3,
  }));
}

export function createClassroomScene(assets) {
  const dust = makeDust();

  const maleWalkStart = 0.8;
  const maleWalkEnd = 5.8;
  const maleSitStart = 6.15;
  const maleSeatX = 330;
  const femaleSeatX = 468;
  const floorY = 320;
  const fgY = 8;

  return {
    id: "classroom",
    draw(ctx, t) {
      const camX = 20 + easeInOut(clamp(t / 11)) * 40; // follows the walk, 20 -> 60
      const look = envelope(t, 7.0, 7.7, 9.7, 10.5);
      const caption = envelope(t, 1.0, 2.1, 4.6, 5.7);

      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(assets.bg.classroom, -Math.round(camX), 0);

      // Window light: a slow breath over the glass, kept very low so the
      // generated background keeps its own texture.
      const lightPulse = 0.07 + Math.sin(t * 0.55) * 0.015;
      blit(ctx, assets.fx.glowWarm, 520 - camX, 150, 300, 230, lightPulse);

      const sx = (x) => x - camX;

      // --- Female: seated, writing, then looks up -------------------------
      const femaleLook = envelope(t, 7.0, 7.6, 9.7, 10.5);
      let femaleFrame;
      if (femaleLook > 0.5) {
        femaleFrame = `look_side_${Math.floor(t * 3) % 2}`;
      } else {
        femaleFrame = `sit_side_${Math.floor(t * 2) % 2}`;
      }
      assets.sprites.draw(ctx, "female", femaleFrame, sx(femaleSeatX), floorY, { flipX: true });

      // --- Male: enters, walks to his seat, sits --------------------------
      if (t < maleWalkStart) {
        // still outside the door
      } else if (t < maleWalkEnd) {
        const q = (t - maleWalkStart) / (maleWalkEnd - maleWalkStart);
        const x = lerp(44, maleSeatX, smoothstep(q));
        const step = (t - maleWalkStart) * 11;
        const frame = Math.floor(step) % 6;
        const bob = frame === 1 || frame === 4 ? -1 : 0;
        assets.sprites.draw(ctx, "male", `walk_side_${frame}`, sx(x), floorY + bob);
      } else if (t < maleSitStart) {
        const frame = Math.floor(t * 4) % 4;
        assets.sprites.draw(ctx, "male", `idle_side_${frame}`, sx(maleSeatX), floorY);
      } else {
        const maleLook = envelope(t, 7.5, 7.9, 9.7, 10.4);
        let frame;
        if (maleLook > 0.5) frame = `look_side_${Math.floor(t * 3) % 2}`;
        else frame = `sit_side_${Math.floor(t * 2) % 2}`;
        assets.sprites.draw(ctx, "male", frame, sx(maleSeatX), floorY);
      }

      // A warm edge on the two figures while the light is strongest.
      ctx.globalAlpha = 0.05 + lightPulse;
      ctx.fillStyle = PAL.warm;
      ctx.fillRect(sx(maleSeatX) - 12, 278, 24, 1);
      ctx.fillRect(sx(femaleSeatX) - 12, 278, 24, 1);
      ctx.globalAlpha = 1;

      // --- Dust in the window light --------------------------------------
      for (const d of dust) {
        const x = ((d.x + Math.sin(t * 0.35 + d.phase) * d.sway - camX * 0.5) % 700 + 700) % 700;
        const y = ((d.y + t * d.speed) % 250) + 60;
        if (x < 330 || x > W + 4) continue;
        const twinkle = 0.35 + 0.65 * Math.sin(t * 1.1 + d.phase);
        ctx.globalAlpha = d.alpha * Math.max(0, twinkle);
        ctx.fillStyle = PAL.warm;
        ctx.fillRect(Math.round(x), Math.round(y), 1, 1);
      }
      ctx.globalAlpha = 1;

      // Foreground desks occlude the seated figures' laps and legs.
      ctx.drawImage(assets.bg.classroomFg, -Math.round(camX), fgY);

      if (caption > 0) {
        ctx.globalAlpha = caption * 0.66;
        drawText(ctx, "高一 · 开学不久", 16, 338, {
          size: 9,
          color: PAL.cream,
          letterSpacing: 1,
          shadow: true,
          shadowColor: PAL.ink,
        });
        ctx.globalAlpha = 1;
      }

      // Eye-contact beat: a single soft warm pixel above each head.  This is
      // the only "effect" in the shot, and it is almost subliminal.
      if (look > 0) {
        ctx.globalAlpha = look * (0.25 + 0.15 * Math.sin(t * 3));
        ctx.fillStyle = PAL.warm;
        ctx.fillRect(Math.round(sx(maleSeatX)), 271, 1, 1);
        ctx.fillRect(Math.round(sx(femaleSeatX)), 271, 1, 1);
        ctx.globalAlpha = 1;
      }

      ctx.restore();
    },
  };
}
