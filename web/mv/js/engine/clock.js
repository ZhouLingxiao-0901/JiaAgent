/* Transport clock for the silent sample.
 *
 * The MV is driven by a monotonic clock rather than by per-frame deltas.  That
 * keeps every animation deterministic: seeking to 12.4 s produces exactly the
 * same picture as letting the sample play to 12.4 s.
 */

export class MVClock {
  constructor(duration, { autoplay = true } = {}) {
    this.duration = duration;
    this.offset = 0;
    this.startedAt = 0;
    this.playing = false;
    this.audioMode = "silent";
    if (autoplay) this.play();
  }

  now() {
    return typeof performance !== "undefined" ? performance.now() : Date.now();
  }

  play() {
    if (this.playing) return;
    if (this.offset >= this.duration) this.offset = 0;
    this.startedAt = this.now();
    this.playing = true;
  }

  pause() {
    if (!this.playing) return;
    this.offset = this.time();
    this.playing = false;
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  reset() {
    this.offset = 0;
    this.startedAt = this.now();
  }

  seek(t) {
    const clamped = Math.max(0, Math.min(this.duration, Number(t) || 0));
    this.offset = clamped;
    this.startedAt = this.now();
  }

  time() {
    if (!this.playing) return this.offset;
    const elapsed = this.offset + (this.now() - this.startedAt) / 1000;
    if (elapsed >= this.duration) {
      this.offset = this.duration;
      this.playing = false;
      return this.duration;
    }
    return elapsed;
  }

  get ended() {
    return this.offset >= this.duration && !this.playing;
  }
}
