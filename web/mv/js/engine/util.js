export const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
export const lerp = (a, b, t) => a + (b - a) * t;

export function smoothstep(t) {
  const x = clamp(t);
  return x * x * (3 - 2 * x);
}

export function easeInOut(t) {
  const x = clamp(t);
  return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
}

/** Fade in over [a,b], hold, fade out over [c,d]. */
export function envelope(t, a, b, c, d) {
  if (t <= a || t >= d) return 0;
  if (t < b) return smoothstep((t - a) / (b - a));
  if (t <= c) return 1;
  return 1 - smoothstep((t - c) / (d - c));
}

/** Small deterministic PRNG so particle fields never flicker between seeks. */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Draw an image centred at (x,y) scaled to an exact pixel size. */
export function blit(ctx, img, x, y, w, h, alpha = 1) {
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha *= alpha;
  ctx.drawImage(img, Math.round(x - w / 2), Math.round(y - h / 2), w, h);
  ctx.restore();
}

export function blitTopLeft(ctx, img, x, y, alpha = 1) {
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha *= alpha;
  ctx.drawImage(img, Math.round(x), Math.round(y));
  ctx.restore();
}
