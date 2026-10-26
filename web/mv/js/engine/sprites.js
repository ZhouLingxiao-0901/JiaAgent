/* Sprite atlas helper.
 *
 * The generated atlas uses 48 x 64 cells with the anchor at (24, 60), so all
 * draw calls take a foot position and the character stays glued to the ground
 * regardless of frame contents.
 */

export class SpriteBank {
  constructor(images, manifest) {
    this.images = images;
    this.manifest = manifest;
    this.anchorX = manifest.anchor[0];
    this.anchorY = manifest.anchor[1];
    this.cellW = manifest.cell[0];
    this.cellH = manifest.cell[1];
  }

  has(who, frame) {
    return Boolean(this.manifest.characters[who]?.[frame]);
  }

  /**
   * Draw a frame with its feet at (x, y).
   * opts: flipX, alpha
   */
  draw(ctx, who, frame, x, y, opts = {}) {
    const rect = this.manifest.characters[who]?.[frame];
    if (!rect) return;

    const [sx, sy, sw, sh] = rect;
    const dx = Math.round(x);
    const dy = Math.round(y);

    ctx.save();
    ctx.imageSmoothingEnabled = false;
    if (opts.alpha !== undefined) ctx.globalAlpha *= opts.alpha;

    if (opts.flipX) {
      ctx.translate(dx, 0);
      ctx.scale(-1, 1);
      ctx.drawImage(
        this.images[who],
        sx, sy, sw, sh,
        -this.anchorX, dy - this.anchorY, sw, sh
      );
    } else {
      ctx.drawImage(
        this.images[who],
        sx, sy, sw, sh,
        dx - this.anchorX, dy - this.anchorY, sw, sh
      );
    }
    ctx.restore();
  }

  /** Pick a frame from a cycle, e.g. walk(who, 2, 6) -> walk_side_2. */
  frame(prefix, index, count) {
    return `${prefix}${((index % count) + count) % count}`;
  }
}
