/* Crisp 1-bit text rendered from the system font.
 *
 * The MV has no external font dependency.  Text is drawn once into an
 * offscreen canvas at its native pixel size, then every pixel is thresholded
 * to fully opaque or fully transparent.  That removes antialiasing and keeps
 * Chinese glyphs legible at 10-12 px without shipping a font file.
 */

const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", "SimHei", sans-serif';
const cache = new Map();
let measureCtx = null;

function getMeasureCtx() {
  if (!measureCtx) {
    const c = document.createElement("canvas");
    measureCtx = c.getContext("2d");
  }
  return measureCtx;
}

function hexToRgb(hex) {
  const v = hex.replace("#", "");
  const n = v.length === 3
    ? v.split("").map((ch) => parseInt(ch + ch, 16))
    : [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
  return n;
}

export function measureText(text, size = 12, letterSpacing = 0) {
  const g = getMeasureCtx();
  g.font = `${size}px ${FONT}`;
  if (!letterSpacing) return Math.ceil(g.measureText(text).width);
  let w = 0;
  for (const ch of text) w += g.measureText(ch).width + letterSpacing;
  return Math.ceil(w);
}

function render(text, size, color, letterSpacing, bold, threshold) {
  const key = `${text}\u0000${size}\u0000${color}\u0000${letterSpacing}\u0000${bold}\u0000${threshold}`;
  const hit = cache.get(key);
  if (hit) return hit;

  const gMeasure = getMeasureCtx();
  gMeasure.font = `${bold ? "bold " : ""}${size}px ${FONT}`;
  let width;
  if (!letterSpacing) {
    width = Math.ceil(gMeasure.measureText(text).width);
  } else {
    width = 0;
    for (const ch of text) width += Math.ceil(gMeasure.measureText(ch).width) + letterSpacing;
    width -= letterSpacing;
  }

  const pad = 4;
  const c = document.createElement("canvas");
  c.width = Math.max(1, width + pad * 2);
  c.height = Math.max(1, size + pad * 2 + 2);
  const g = c.getContext("2d", { willReadFrequently: true });
  g.font = `${bold ? "bold " : ""}${size}px ${FONT}`;
  g.textBaseline = "top";
  g.fillStyle = "#ffffff";

  if (!letterSpacing) {
    g.fillText(text, pad, pad);
    if (bold) g.fillText(text, pad + 1, pad);
  } else {
    let x = pad;
    for (const ch of text) {
      g.fillText(ch, x, pad);
      if (bold) g.fillText(ch, x + 1, pad);
      x += Math.ceil(g.measureText(ch).width) + letterSpacing;
    }
  }

  const img = g.getImageData(0, 0, c.width, c.height);
  const [r, gg, b] = hexToRgb(color);
  for (let i = 0; i < img.data.length; i += 4) {
    if (img.data[i + 3] > threshold) {
      img.data[i] = r;
      img.data[i + 1] = gg;
      img.data[i + 2] = b;
      img.data[i + 3] = 255;
    } else {
      img.data[i + 3] = 0;
    }
  }
  g.putImageData(img, 0, 0);

  const entry = { canvas: c, width, height: c.height, pad };
  cache.set(key, entry);
  return entry;
}

/**
 * Draw pixel text.
 * opts: size, color, align, letterSpacing, bold, shadow, shadowColor,
 *       reveal (0..1), alpha
 * Returns the untrimmed text width (without padding).
 */
export function drawText(ctx, text, x, y, opts = {}) {
  if (!text) return 0;
  const {
    size = 12,
    color = "#f2eee2",
    align = "left",
    letterSpacing = 0,
    bold = false,
    shadow = false,
    shadowColor = "#1a1720",
    reveal = 1,
    alpha = 1,
    threshold = 72,
  } = opts;

  const entry = render(text, size, color, letterSpacing, bold, threshold);
  const shadowEntry = shadow
    ? render(text, size, shadowColor, letterSpacing, bold, threshold)
    : null;

  let dx = Math.round(x);
  if (align === "center") dx -= Math.round(entry.width / 2);
  else if (align === "right") dx -= entry.width;
  const dy = Math.round(y);

  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha *= alpha;

  if (reveal >= 1) {
    if (shadowEntry) {
      ctx.drawImage(shadowEntry.canvas, dx - shadowEntry.pad + 1, dy - shadowEntry.pad + 1);
    }
    ctx.drawImage(entry.canvas, dx - entry.pad, dy - entry.pad);
  } else {
    const w = Math.max(0, Math.round(entry.width * reveal));
    if (w > 0) {
      ctx.beginPath();
      ctx.rect(dx, dy - entry.pad, w + entry.pad, entry.height + 4);
      ctx.clip();
      if (shadowEntry) {
        ctx.drawImage(shadowEntry.canvas, dx - shadowEntry.pad + 1, dy - shadowEntry.pad + 1);
      }
      ctx.drawImage(entry.canvas, dx - entry.pad, dy - entry.pad);
    }
  }

  ctx.restore();
  return entry.width;
}
