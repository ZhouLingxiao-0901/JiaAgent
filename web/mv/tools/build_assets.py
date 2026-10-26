"""Build all pixel-art assets for the MV sample.

Everything here is generated inside the project.  No external images, fonts or
audio are read.  The script is deterministic: running it twice produces the
same files (except PNG metadata timestamps, if any).

Outputs:
    assets/sprites/male.png
    assets/sprites/female.png
    assets/sprites/manifest.json
    assets/bg/gate_dusk.png
    assets/bg/gate_dusk_fg.png
    assets/bg/classroom_day.png
    assets/bg/classroom_day_fg.png
    assets/bg/bedroom_male_night.png
    assets/bg/bedroom_female_night.png
    assets/fx/glow_blue.png
    assets/fx/glow_warm.png
    assets/fx/glow_moon.png
"""

from __future__ import annotations

import json
import math
import os
import random
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
SPRITES = os.path.join(ASSETS, "sprites")
BG = os.path.join(ASSETS, "bg")
FX = os.path.join(ASSETS, "fx")

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

PAL = {
    "ink":       (26, 23, 32),
    "ink2":      (42, 36, 51),
    "hair":      (34, 31, 42),
    "hair_hi":   (58, 51, 68),
    "hair2":     (42, 34, 48),
    "hair2_hi":  (74, 58, 68),
    "skin":      (244, 203, 166),
    "skin_sh":   (221, 165, 126),
    "skin_deep": (185, 123, 92),
    "eye":       (42, 34, 48),
    "blush":     (217, 122, 108),
    "navy":      (47, 63, 96),
    "navy_dk":   (31, 42, 68),
    "navy_hi":   (74, 95, 134),
    "white":     (242, 238, 226),
    "white_sh":  (207, 200, 183),
    "white_dk":  (157, 151, 138),
    "red":       (180, 71, 62),
    "red_hi":    (201, 86, 79),
    "red_soft":  (224, 138, 127),
    "warm":      (255, 217, 160),
    "warm_mid":  (242, 181, 107),
    "warm_dk":   (201, 133, 74),
    "cool":      (159, 198, 216),
    "cool_mid":  (111, 152, 180),
    "cool_dk":   (69, 107, 134),
    "green":     (63, 93, 74),
    "green_mid": (95, 127, 92),
    "green_hi":  (136, 160, 110),
    "brown":     (74, 58, 48),
    "brown_mid": (107, 84, 67),
    "brown_hi":  (141, 112, 88),
    "gold":      (217, 164, 65),
    "dusk":      (142, 111, 158),
    "plum":      (90, 65, 88),
    "rose":      (122, 74, 88),
    "cream":     (232, 216, 200),
    "night":     (26, 32, 53),
    "night2":    (38, 44, 68),
    "floor":     (88, 70, 64),
    "floor_hi":  (116, 92, 78),
}


def C(name: str):
    return PAL[name]


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------

BAYER4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def rect(d: ImageDraw.ImageDraw, x0, y0, x1, y1, color):
    """Half-open rectangle: x1/y1 are exclusive."""
    if x1 <= x0 or y1 <= y0:
        return
    d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=color)


def hline(d, x0, x1, y, color):
    rect(d, x0, y, x1, y + 1, color)


def vline(d, x, y0, y1, color):
    rect(d, x, y0, x + 1, y1, color)


def dither_rect(img, x0, y0, x1, y1, c1, c2, scale=1):
    d = ImageDraw.Draw(img)
    for y in range(y0, y1):
        for x in range(x0, x1):
            t = BAYER4[(y // scale) % 4][(x // scale) % 4] / 16.0
            d.point((x, y), fill=c1 if t < 0.5 else c2)


def dither_gradient(img, x0, y0, x1, y1, stops, scale=2):
    """stops: [(position 0..1, color), ...] sorted by position."""
    d = ImageDraw.Draw(img)
    for y in range(y0, y1):
        p = (y - y0) / max(1, (y1 - y0 - 1))
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= p <= p1:
                local = (p - p0) / max(1e-6, (p1 - p0))
                for x in range(x0, x1):
                    t = BAYER4[(y // scale) % 4][(x // scale) % 4] / 16.0
                    d.point((x, y), fill=c0 if local < t else c1)
                break


def dither_circle(img, cx, cy, r, inner, outer, density=0.5, scale=1):
    d = ImageDraw.Draw(img)
    r2 = r * r
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= r2:
                t = BAYER4[(y // scale) % 4][(x // scale) % 4] / 16.0
                d.point((x, y), fill=inner if t < density else outer)


def outlined_ellipse(d, box, fill, outline, width=1):
    d.ellipse(box, fill=outline)
    x0, y0, x1, y1 = box
    d.ellipse([x0 + width, y0 + width, x1 - width, y1 - width], fill=fill)


# ---------------------------------------------------------------------------
# Character pixel maps
# ---------------------------------------------------------------------------

MALE_MAP = {
    ".": None,
    "o": C("ink"),
    "h": C("hair"),
    "H": C("hair_hi"),
    "s": C("skin"),
    "S": C("skin_sh"),
    "D": C("skin_deep"),
    "e": C("eye"),
    "n": C("navy"),
    "N": C("navy_dk"),
    "l": C("navy_hi"),
    "w": C("white"),
    "W": C("white_sh"),
    "r": C("red"),
    "R": C("red_hi"),
}

FEMALE_MAP = dict(MALE_MAP)
FEMALE_MAP.update({
    "h": C("hair2"),
    "H": C("hair2_hi"),
    "f": C("hair2"),
    "F": C("hair2_hi"),
})


def draw_map(img, rows, x, y, mapping):
    d = ImageDraw.Draw(img)
    for yy, row in enumerate(rows):
        for xx, ch in enumerate(row):
            color = mapping.get(ch)
            if color is not None:
                d.point((x + xx, y + yy), fill=color)


MALE_HEAD_SIDE = [
    ".....oooooo.....",
    "...oohhhhhhoo...",
    "..ohhhhhhhhhho..",
    ".ohhHHhhhhhhho..",
    ".ohhhhhhhhhhho..",
    ".ohhssssssssho..",
    ".ohhssssssssho..",
    ".ohhssssssesho..",
    ".ohhsssssssSho..",
    ".ohhsssssssSho..",
    "..ohsssssshho...",
    "...oohhhhoo.....",
    "....oooooo......",
    "................",
    "................",
    "................",
]

MALE_HEAD_FRONT = [
    ".....oooooo.....",
    "...oohhhhhhoo...",
    "..ohhHHhhhhhho..",
    ".ohhhhhhhhhhhho.",
    ".ohhhhhhhhhhhho.",
    ".ohhsssssssshho.",
    ".ohssssssssssho.",
    ".ohsessssssesho.",
    ".ohssssssssssho.",
    ".ohsssSSssssho..",
    "..ohssssssssho..",
    "...oohsssshoo...",
    ".....oooooo.....",
    "......osso......",
    "......osso......",
]

FEMALE_HEAD_SIDE = [
    ".....oooooo.....",
    "...oofffffhoo...",
    "..offfFFffffho..",
    ".offffffffffho..",
    ".offffffffffho..",
    ".offssssssssho..",
    ".offssssssssho..",
    ".offssssssesho..",
    ".offsssssssSho..",
    ".offsssssssSho..",
    "..ofsssssssho...",
    "...ooffffhoo....",
    "....offffo......",
    "....offo........",
    "...offo.........",
    "...ooo..........",
]

FEMALE_HEAD_FRONT = [
    ".....oooooo.....",
    "...oofffffhoo...",
    "..offfFFffffho..",
    ".offffffffffho..",
    ".offffffffffho..",
    ".offssssssssho..",
    ".ofssssssssssho.",
    ".ofsessssssesho.",
    ".ofssssssssssho.",
    ".ofsssSSssssho..",
    "..ofssssssssho..",
    "...ooffssffoo...",
    ".....oooooo.....",
    "....offo.offo...",
    "....offo.offo...",
    ".....oo...oo....",
]

MALE_TORSO_SIDE = [
    "..oooooooo..",
    ".onnnnnnnno.",
    ".olllnnnnno.",
    ".onnnnnnwwno",
    ".onnnnnnwwno",
    ".onnnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".onnnnnnnnno",
    "..onnnnnnno.",
    "..onnnnnnno.",
    "..oooooooo..",
]

FEMALE_TORSO_SIDE = [
    "..oooooooo..",
    ".onnnnnnnno.",
    ".olllnnnnno.",
    ".onnnnnnrRno",
    ".onnnnnnrrno",
    ".onnnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".owwnnnnnnno",
    ".onnnnnnnnno",
    ".onnnnnnnnno",
    "..onnnnnnno.",
    "..onnnnnnno.",
    "..oooooooo..",
]

MALE_TORSO_FRONT = [
    "..oooooooooo..",
    ".onnnnnnnnnno.",
    ".onnnnnnnnnno.",
    ".onwwnnnnwwwno",
    ".onwwnnnnwwwno",
    ".onnnnnnnnnno.",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnnnnnnno.",
    ".onnnnnnnnnno.",
    "..onnnnnnnno..",
    "...oooooooo...",
]

FEMALE_TORSO_FRONT = [
    "..oooooooooo..",
    ".onnnnnnnnnno.",
    ".onnnnnnnnnno.",
    ".onnnnnrrnnnno",
    ".onnnnnrrnnnno",
    ".onnnnnnnnnno.",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnwwnnnnno",
    ".onnnnnnnnnno.",
    ".onnnnnnnnnno.",
    ".onnnnnnnnnno.",
    "..onnnnnnnno..",
    "...oooooooo...",
]


# ---------------------------------------------------------------------------
# Limbs
# ---------------------------------------------------------------------------

def thick_line(d, points, width, fill, outline, highlight=None):
    """Outlined pixel limb.  Points are (x, y) tuples."""
    d.line(points, fill=outline, width=width + 2, joint="curve")
    d.line(points, fill=fill, width=width, joint="curve")
    if highlight:
        shifted = [(x - 1, y) for (x, y) in points]
        d.line(shifted, fill=highlight, width=1, joint="curve")


def draw_ponytail(img, x, y, small=False):
    """Tapered low ponytail with a red tie, drawn behind the head."""
    d = ImageDraw.Draw(img)
    if small:
        d.polygon([(x, y + 1), (x + 4, y + 1), (x + 4, y + 6),
                   (x + 2, y + 9), (x, y + 6)], fill=C("ink"))
        d.polygon([(x + 1, y + 2), (x + 3, y + 2), (x + 3, y + 5),
                   (x + 2, y + 7), (x + 1, y + 5)], fill=C("hair2"))
        d.point((x + 1, y + 3), fill=C("hair2_hi"))
        rect(d, x, y, x + 4, y + 2, C("red"))
        return

    d.polygon([(x + 1, y), (x + 5, y), (x + 6, y + 4), (x + 5, y + 9),
               (x + 3, y + 12), (x + 1, y + 9), (x, y + 4)], fill=C("ink"))
    d.polygon([(x + 2, y + 1), (x + 4, y + 1), (x + 5, y + 4), (x + 4, y + 8),
               (x + 3, y + 10), (x + 2, y + 8), (x + 1, y + 4)], fill=C("hair2"))
    d.line([(x + 2, y + 2), (x + 2, y + 7)], fill=C("hair2_hi"))
    rect(d, x + 1, y, x + 5, y + 2, C("red"))
    d.point((x + 3, y + 1), fill=C("red_hi"))


def draw_side_arm(img, x, y, pose, far=False, female=False):
    """x,y = shoulder.  Arm hangs to the right side of a right-facing body."""
    d = ImageDraw.Draw(img)
    fill = C("navy_dk") if far else C("navy")
    hi = C("navy") if far else C("navy_hi")
    outline = C("ink")

    if pose == "forward":
        pts = [(x, y), (x + 2, y + 5), (x + 3, y + 10)]
    elif pose == "back":
        pts = [(x, y), (x - 1, y + 5), (x - 2, y + 10)]
    elif pose == "mid":
        pts = [(x, y), (x + 1, y + 5), (x + 1, y + 10)]
    elif pose == "desk":
        pts = [(x, y), (x + 3, y + 6), (x + 8, y + 8)]
    elif pose == "phone":
        pts = [(x, y), (x + 1, y + 7), (x + 5, y + 4)]
    else:
        pts = [(x, y), (x, y + 5), (x, y + 10)]

    thick_line(d, pts, 3, fill, outline, hi)
    hx, hy = pts[-1]
    rect(d, hx - 1, hy, hx + 2, hy + 3, C("skin"))
    d.point((hx - 1, hy + 3), fill=C("ink"))
    d.point((hx + 2, hy + 3), fill=C("ink"))


def draw_side_leg(img, x, y, pose, far=False, female=False):
    """x = hip centre, y = hip top.  Legs end at y+14."""
    d = ImageDraw.Draw(img)
    fill = C("navy_dk") if far else C("navy")
    hi = C("navy") if far else C("navy_hi")
    outline = C("ink")

    dx = {"forward": 2, "back": -2, "mid": 1, "stand": 0}.get(pose, 0)
    hip = (x, y)
    knee = (x + dx, y + 7)
    ankle = (x + dx * 2, y + 13)
    thick_line(d, [hip, knee, ankle], 4, fill, outline, hi)

    # shoe
    sx = x + dx * 2 - 2
    sy = y + 12
    rect(d, sx, sy, sx + 8, sy + 3, C("ink"))
    rect(d, sx + 1, sy, sx + 7, sy + 2, C("white"))
    rect(d, sx + 1, sy + 2, sx + 7, sy + 3, C("white_sh"))
    d.point((sx + 7, sy + 1), fill=C("ink"))


def draw_front_arm(img, x, y, pose, far=False, female=False):
    d = ImageDraw.Draw(img)
    fill = C("navy_dk") if far else C("navy")
    hi = C("navy") if far else C("navy_hi")
    outline = C("ink")

    if pose == "phone":
        pts = [(x, y), (x + 2, y + 6), (x + 5, y + 4)]
    elif pose == "desk":
        pts = [(x, y), (x + 1, y + 6), (x + 3, y + 10)]
    elif pose == "down":
        pts = [(x, y), (x, y + 6), (x, y + 11)]
    else:
        pts = [(x, y), (x, y + 6), (x, y + 11)]

    thick_line(d, pts, 3, fill, outline, hi)
    hx, hy = pts[-1]
    rect(d, hx - 1, hy, hx + 2, hy + 3, C("skin"))
    d.point((hx - 1, hy + 3), fill=C("ink"))
    d.point((hx + 2, hy + 3), fill=C("ink"))


# ---------------------------------------------------------------------------
# Character composition
# ---------------------------------------------------------------------------

CELL_W, CELL_H = 48, 64
CX, FEET = 24, 60


def draw_character(img, female, pose, frame, blink=False):
    """Draw one 48x64 cell.

    pose:
        idle_side, walk_side, sit_side, look_side,
        phone_front, look_front
    """
    d = ImageDraw.Draw(img)
    mapping = FEMALE_MAP if female else MALE_MAP

    head_side = FEMALE_HEAD_SIDE if female else MALE_HEAD_SIDE
    head_front = FEMALE_HEAD_FRONT if female else MALE_HEAD_FRONT
    torso_side = FEMALE_TORSO_SIDE if female else MALE_TORSO_SIDE
    torso_front = FEMALE_TORSO_FRONT if female else MALE_TORSO_FRONT

    # Base vertical layout
    if female:
        stand_head_y = 16
        torso_h = 16
    else:
        stand_head_y = 12
        torso_h = 16

    side = pose in ("idle_side", "walk_side", "sit_side", "look_side")
    sit = pose == "sit_side"

    # bob and sitting offset
    if pose == "walk_side":
        bob = 1 if frame in (1, 4) else 0
        body_y = stand_head_y - bob
    elif pose == "idle_side":
        bob = 1 if frame in (1, 3) else 0
        body_y = stand_head_y - bob
    elif pose == "look_side":
        body_y = stand_head_y - 1
    elif sit:
        body_y = stand_head_y + 7
    elif pose == "phone_front":
        body_y = stand_head_y + (1 if frame in (1, 3) else 0)
    elif pose == "look_front":
        body_y = stand_head_y - 1
    else:
        body_y = stand_head_y

    head_y = body_y
    torso_y = head_y + 18

    # ---- back layers -----------------------------------------------------
    if side:
        # A bag is only worn while walking.  Keeping it off the seated and
        # idle frames makes the silhouette much cleaner (and avoids a brown
        # slab floating behind the shoulder).
        if pose == "walk_side" and not female:
            bx, by = CX - 11, torso_y + 4
            rect(d, bx, by, bx + 6, by + 9, C("ink"))
            rect(d, bx + 1, by + 1, bx + 5, by + 8, C("brown"))
            rect(d, bx + 1, by + 1, bx + 2, by + 8, C("brown_mid"))
            rect(d, bx + 4, by + 3, bx + 5, by + 5, C("brown_hi"))
            # shoulder strap
            d.line([(bx + 5, by), (CX - 2, torso_y + 2)], fill=C("brown"), width=1)
        elif pose == "walk_side" and female:
            bx, by = CX - 11, torso_y + 5
            rect(d, bx, by, bx + 6, by + 7, C("ink"))
            rect(d, bx + 1, by + 1, bx + 5, by + 6, C("cream"))
            rect(d, bx + 1, by + 1, bx + 5, by + 2, C("white_sh"))
            rect(d, bx + 2, by + 4, bx + 4, by + 5, C("brown_hi"))
            d.line([(bx + 5, by), (CX - 1, torso_y + 2)], fill=C("cream"), width=1)

        # ponytail behind head
        if female:
            draw_ponytail(img, CX - 11, head_y + 7)

        # far leg / far arm
        if not sit:
            draw_side_leg(img, CX - 2, torso_y + torso_h - 2, "back" if frame % 2 == 0 else "forward",
                          far=True, female=female)
        draw_side_arm(img, CX - 4, torso_y + 3, "back" if frame % 2 == 0 else "forward",
                      far=True, female=female)

    # ---- torso -----------------------------------------------------------
    if side:
        draw_map(img, torso_side, CX - 6, torso_y, mapping)
    else:
        draw_map(img, torso_front, CX - 7, torso_y, mapping)

    # ---- head ------------------------------------------------------------
    if side:
        draw_map(img, head_side, CX - 8, head_y, mapping)
    else:
        draw_map(img, head_front, CX - 8, head_y, mapping)
        if female:
            # low ponytail peeking on the right
            draw_ponytail(img, CX + 5, head_y + 8, small=True)

    # blink: erase the eye with skin shadow
    if blink:
        if side:
            rect(d, CX + 2, head_y + 7, CX + 4, head_y + 8, C("skin_sh"))
        else:
            rect(d, CX - 4, head_y + 7, CX - 2, head_y + 8, C("skin_sh"))
            rect(d, CX + 1, head_y + 7, CX + 3, head_y + 8, C("skin_sh"))

    # ---- front/near layers ----------------------------------------------
    if side:
        if not sit:
            draw_side_leg(img, CX + 2, torso_y + torso_h - 2,
                          "forward" if frame % 2 == 0 else "back",
                          far=False, female=female)

        if sit:
            # arm resting on desk
            draw_side_arm(img, CX + 1, torso_y + 3, "desk",
                          far=False, female=female)
        elif pose == "walk_side":
            draw_side_arm(img, CX + 1, torso_y + 3,
                          "forward" if frame % 2 == 0 else "back",
                          far=False, female=female)
        else:
            draw_side_arm(img, CX + 1, torso_y + 3, "mid",
                          far=False, female=female)

        # face/hair touch at close range: a tiny ear
        if female:
            d.point((CX + 7, head_y + 8), fill=C("skin_sh"))
    else:
        # front arms
        if pose == "phone_front":
            draw_front_arm(img, CX - 8, torso_y + 3, "phone", far=True, female=female)
            draw_front_arm(img, CX + 5, torso_y + 3, "phone", far=False, female=female)
            # phone in hand
            pxx, pyy = CX - 2, torso_y + 10
            rect(d, pxx, pyy, pxx + 5, pyy + 9, C("ink"))
            rect(d, pxx + 1, pyy + 1, pxx + 4, pyy + 8, C("navy_dk"))
            rect(d, pxx + 1, pyy + 1, pxx + 4, pyy + 6, C("cool"))
            rect(d, pxx + 1, pyy + 1, pxx + 4, pyy + 2, C("white"))
            # thumbs
            rect(d, pxx - 1, pyy + 4, pxx + 1, pyy + 6, C("skin"))
            rect(d, pxx + 4, pyy + 4, pxx + 6, pyy + 6, C("skin"))
        elif pose == "look_front":
            draw_front_arm(img, CX - 8, torso_y + 3, "down", far=True, female=female)
            draw_front_arm(img, CX + 5, torso_y + 3, "down", far=False, female=female)
        else:
            draw_front_arm(img, CX - 8, torso_y + 3, "down", far=True, female=female)
            draw_front_arm(img, CX + 5, torso_y + 3, "down", far=False, female=female)


FRAME_ROWS = {
    "male": [
        ["idle_side_0", "idle_side_1", "idle_side_2", "idle_side_3", "look_side_0", "look_side_1"],
        ["walk_side_0", "walk_side_1", "walk_side_2", "walk_side_3", "walk_side_4", "walk_side_5"],
        ["sit_side_0", "sit_side_1", "sit_side_2", "sit_side_3", "phone_front_0", "phone_front_1"],
        ["phone_front_2", "phone_front_3", "look_front_0", "look_front_1", "", ""],
    ],
    "female": [
        ["idle_side_0", "idle_side_1", "idle_side_2", "idle_side_3", "look_side_0", "look_side_1"],
        ["walk_side_0", "walk_side_1", "walk_side_2", "walk_side_3", "walk_side_4", "walk_side_5"],
        ["sit_side_0", "sit_side_1", "sit_side_2", "sit_side_3", "phone_front_0", "phone_front_1"],
        ["phone_front_2", "phone_front_3", "look_front_0", "look_front_1", "", ""],
    ],
}


def frame_spec(name: str):
    """Map a frame name to (pose, frame_index, blink)."""
    pose_sets = {
        "idle_side": ("idle_side", [False, False, False, True]),
        "walk_side": ("walk_side", [False, False, False, False, False, False]),
        "sit_side": ("sit_side", [False, False, False, True]),
        "look_side": ("look_side", [False, True]),
        "phone_front": ("phone_front", [False, False, True, False]),
        "look_front": ("look_front", [False, True]),
    }
    for prefix, (pose, blinks) in pose_sets.items():
        if name.startswith(prefix + "_"):
            idx = int(name.rsplit("_", 1)[1])
            return pose, idx, blinks[idx]
    raise ValueError(name)


def build_sprite_sheet(name: str, female: bool):
    rows = FRAME_ROWS[name]
    cols = 6
    sheet = Image.new("RGBA", (CELL_W * cols, CELL_H * len(rows)), (0, 0, 0, 0))
    manifest = {}

    for r, row in enumerate(rows):
        for c, frame_name in enumerate(row):
            if not frame_name:
                continue
            pose, idx, blink = frame_spec(frame_name)
            cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
            draw_character(cell, female, pose, idx, blink=blink)
            sheet.paste(cell, (c * CELL_W, r * CELL_H), cell)
            manifest[frame_name] = [c * CELL_W, r * CELL_H, CELL_W, CELL_H]

    sheet.save(os.path.join(SPRITES, f"{name}.png"))

    # debug preview at 4x
    prev = sheet.resize((sheet.width * 4, sheet.height * 4), Image.NEAREST)
    bgprev = Image.new("RGBA", prev.size, (238, 232, 218, 255))
    bgprev.alpha_composite(prev)
    bgprev.save(os.path.join(SPRITES, f"_{name}_preview.png"))

    return manifest


# ---------------------------------------------------------------------------
# Background: dusk school gate
# ---------------------------------------------------------------------------

def draw_sun(img, cx, cy, r):
    d = ImageDraw.Draw(img)
    # halo, dithered
    for y in range(cy - 70, cy + 70):
        for x in range(cx - 70, cx + 70):
            dx, dy = x - cx, y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            if dist <= r:
                d.point((x, y), fill=C("warm"))
            elif dist <= r + 4:
                d.point((x, y), fill=C("warm_mid"))
            elif dist <= r + 26:
                t = BAYER4[y % 4][x % 4] / 16.0
                if t < 0.45:
                    d.point((x, y), fill=C("warm_mid"))
            elif dist <= r + 58:
                t = BAYER4[y % 4][x % 4] / 16.0
                if t < 0.16:
                    d.point((x, y), fill=C("warm_dk"))


def draw_tree(img, cx, base_y, height, width, seed):
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    # trunk
    trunk_w = max(4, width // 14)
    d.polygon([(cx - trunk_w, base_y), (cx + trunk_w, base_y),
               (cx + trunk_w - 1, base_y - height), (cx - trunk_w + 1, base_y - height)],
              fill=C("brown"))
    d.line([(cx - trunk_w + 1, base_y), (cx - trunk_w + 2, base_y - height + 4)],
           fill=C("brown_mid"), width=2)
    # branches
    for i in range(3):
        by = base_y - height + 20 + i * 22
        d.line([(cx, by), (cx - width // 3 + i * 3, by - 18)], fill=C("brown"), width=3)
        d.line([(cx + 1, by - 2), (cx + width // 3 - i * 2, by - 22)], fill=C("brown"), width=3)
    # canopy blobs
    top = base_y - height - 20
    for _ in range(95):
        bx = cx + rnd.randint(-width // 2, width // 2)
        by = base_y - height + rnd.randint(-10, 60)
        rr = rnd.randint(7, 20)
        if by < top:
            continue
        col = rnd.choice([C("green"), C("green_mid"), C("green_mid"), C("green_hi")])
        d.ellipse([bx - rr, by - rr // 2, bx + rr, by + rr // 2], fill=col)
    # highlight leaves
    for _ in range(40):
        bx = cx + rnd.randint(-width // 2, width // 2)
        by = base_y - height + rnd.randint(-6, 40)
        d.rectangle([bx, by, bx + rnd.randint(1, 3), by + rnd.randint(1, 2)], fill=C("green_hi"))


def draw_building(img, x0, y0, w, h, wall, roof, seed, lit=True):
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    rect(d, x0, y0, x0 + w, y0 + h, wall)
    rect(d, x0 - 4, y0 - 8, x0 + w + 4, y0, roof)
    rect(d, x0 - 5, y0 - 10, x0 + w + 5, y0 - 8, C("ink2"))
    # windows
    cols = max(1, (w - 16) // 22)
    rows = max(1, (h - 24) // 24)
    for r in range(rows):
        for c in range(cols):
            wx = x0 + 10 + c * 22
            wy = y0 + 14 + r * 24
            on = lit and rnd.random() < 0.55
            rect(d, wx, wy, wx + 12, wy + 14, C("ink2"))
            rect(d, wx + 1, wy + 1, wx + 11, wy + 13,
                 C("warm") if on else C("night"))
            rect(d, wx + 1, wy + 1, wx + 11, wy + 2, C("warm_mid") if on else C("night2"))
            # mullion
            vline(d, wx + 6, wy + 1, wy + 13, C("brown"))
    # ledge
    rect(d, x0 - 2, y0 + h - 4, x0 + w + 2, y0 + h, C("brown_mid"))


def draw_gate(width=720, height=360):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)

    # sky
    dither_gradient(img, 0, 0, width, 218, [
        (0.00, C("night2")),
        (0.18, (47, 49, 86)),
        (0.36, (74, 61, 99)),
        (0.54, C("plum")),
        (0.70, (154, 95, 104)),
        (0.82, (201, 122, 102)),
        (0.92, C("warm_mid")),
        (1.00, C("warm")),
    ], scale=2)

    draw_sun(img, 626, 132, 18)

    # clouds
    rnd = random.Random(11)
    for i, (cx, cy, cw, col) in enumerate([
        (120, 72, 150, (78, 70, 105)),
        (360, 52, 180, (92, 78, 112)),
        (560, 110, 130, (122, 90, 112)),
        (210, 130, 120, (144, 102, 112)),
    ]):
        for _ in range(8):
            ox = rnd.randint(-cw // 2, cw // 2)
            oy = rnd.randint(-6, 6)
            rr = rnd.randint(10, 26)
            d.ellipse([cx + ox - rr, cy + oy - rr // 3, cx + ox + rr, cy + oy + rr // 3], fill=col)
        dither_rect(img, cx - cw // 2, cy + 4, cx + cw // 2, cy + 9, col, (170, 120, 120), scale=1)

    # distant city
    for i in range(26):
        bx = i * 30 + rnd.randint(-6, 6)
        bw = rnd.randint(14, 30)
        bh = rnd.randint(18, 62)
        rect(d, bx, 218 - bh, bx + bw, 224, (50, 45, 72))
        for wy in range(218 - bh + 6, 216, 10):
            for wx in range(bx + 4, bx + bw - 3, 8):
                if rnd.random() < 0.35:
                    d.point((wx, wy), fill=C("warm_mid"))

    # school building
    draw_building(img, 118, 112, 486, 128, (101, 80, 88), (60, 47, 62), 3)
    # entrance
    rect(d, 330, 190, 392, 240, C("ink2"))
    rect(d, 336, 198, 386, 240, C("warm_dk"))
    rect(d, 340, 202, 382, 240, C("warm_mid"))
    rect(d, 343, 206, 361, 240, C("night2"))
    rect(d, 365, 206, 379, 240, C("night2"))
    # steps
    rect(d, 322, 240, 400, 246, C("white_dk"))
    rect(d, 318, 246, 404, 250, C("brown_mid"))

    # gate pillars
    for px in (236, 486):
        rect(d, px - 12, 180, px + 12, 300, C("ink2"))
        rect(d, px - 10, 182, px + 10, 300, C("brown_hi"))
        rect(d, px - 10, 182, px - 4, 300, C("brown_mid"))
        rect(d, px - 14, 174, px + 14, 182, C("ink"))
        rect(d, px - 12, 176, px + 12, 181, C("brown_hi"))
        rect(d, px - 7, 196, px + 7, 210, C("cream"))
        rect(d, px - 6, 197, px + 6, 209, C("red"))
        rect(d, px - 2, 200, px + 2, 206, C("warm"))

    # iron fence
    for bx in range(250, 486, 12):
        vline(d, bx, 220, 298, C("ink2"))
        vline(d, bx + 1, 220, 298, C("brown"))
        d.point((bx, 220), fill=C("brown_hi"))
    hline(d, 248, 488, 222, C("brown"))
    hline(d, 248, 488, 260, C("brown"))
    hline(d, 248, 488, 296, C("brown"))

    # sign board
    rect(d, 292, 150, 430, 176, C("ink"))
    rect(d, 295, 153, 427, 173, C("cream"))
    rect(d, 298, 156, 424, 170, C("white"))
    # abstract word shapes (no real school name)
    for i in range(4):
        bx = 312 + i * 28
        rect(d, bx, 159, bx + 14, 167, C("red"))
        rect(d, bx + 3, 160, bx + 11, 162, C("warm"))
        rect(d, bx + 4, 164, bx + 10, 166, C("red_hi"))

    # trees
    draw_tree(img, 60, 300, 120, 150, 21)
    draw_tree(img, 640, 305, 135, 160, 22)

    # ground
    rect(d, 0, 298, width, 314, (110, 91, 83))
    rect(d, 0, 300, width, 306, (127, 104, 88))
    rect(d, 0, 314, width, 340, C("floor"))
    rect(d, 0, 340, width, height, (58, 49, 58))
    # sidewalk tiles
    for x in range(0, width, 48):
        vline(d, x, 300, 314, (96, 80, 74))
    # road markings
    for x in range(30, width, 90):
        rect(d, x, 350, x + 40, 354, (120, 104, 80))
    # curb
    hline(d, 0, width, 314, C("white_dk"))

    # soft shadows and warm pools (kept subtle; no crosswalk-like stripes)
    for (sx0, sy0, sx1, sy1) in [
        (8, 300, 180, 342), (580, 298, 720, 344),
        (238, 300, 288, 360), (456, 300, 506, 360),
    ]:
        d.ellipse([sx0, sy0, sx1, sy1], fill=(82, 69, 72))
    dither_circle(img, 150, 330, 34, (116, 92, 78), (104, 84, 74), density=0.20, scale=2)

    # parked bicycle by the left tree
    for bx in (136, 184):
        d.ellipse([bx, 326, bx + 24, 350], outline=C("ink2"), width=2)
        d.ellipse([bx + 4, 330, bx + 20, 346], outline=C("brown"), width=1)
    d.line([(148, 338), (168, 314), (196, 338)], fill=C("ink2"), width=2)
    d.line([(168, 314), (160, 338), (184, 338)], fill=C("ink2"), width=2)
    d.line([(168, 314), (164, 306)], fill=C("ink2"), width=2)
    hline(d, 158, 172, 305, C("ink2"))

    # lamp post
    vline(d, 690, 150, 305, C("ink2"))
    vline(d, 691, 150, 305, C("brown"))
    rect(d, 681, 146, 701, 150, C("ink2"))
    rect(d, 684, 147, 698, 151, C("warm"))
    dither_circle(img, 691, 160, 26, C("warm_mid"), (120, 90, 80), density=0.18, scale=1)

    # left lamp, lighting the sidewalk
    vline(d, 112, 168, 305, C("ink2"))
    vline(d, 113, 168, 305, C("brown"))
    rect(d, 102, 164, 122, 168, C("ink2"))
    rect(d, 105, 165, 119, 169, C("warm"))
    dither_circle(img, 112, 178, 24, C("warm_mid"), (110, 86, 78), density=0.16, scale=1)

    # utility pole + wires
    vline(d, 708, 80, 305, C("ink2"))
    vline(d, 709, 80, 305, C("brown"))
    hline(d, 708, 712, 96, C("brown_mid"))
    d.line([(708, 92), (640, 110), (560, 100)], fill=C("ink2"), width=1)
    d.line([(708, 104), (620, 126), (540, 118)], fill=C("ink2"), width=1)

    # fallen leaves
    for _ in range(40):
        x = rnd.randint(0, width - 1)
        y = rnd.randint(305, 358)
        col = rnd.choice([C("warm_dk"), C("red"), C("brown_hi")])
        rect(d, x, y, x + 2, y + 1, col)

    return img


def draw_gate_fg(width=720, height=360):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rnd = random.Random(31)
    # dark foreground leaves at the top corners
    for corner in ("tl", "tr", "bl", "br"):
        for _ in range(70):
            if corner == "tl":
                x = rnd.randint(-10, 150); y = rnd.randint(-10, 90)
            elif corner == "tr":
                x = rnd.randint(width - 150, width + 10); y = rnd.randint(-10, 90)
            elif corner == "bl":
                x = rnd.randint(-10, 90); y = rnd.randint(height - 70, height + 10)
            else:
                x = rnd.randint(width - 90, width + 10); y = rnd.randint(height - 70, height + 10)
            rr = rnd.randint(3, 9)
            col = rnd.choice([(22, 26, 32), (30, 38, 38), (24, 32, 34)])
            d.ellipse([x - rr, y - rr // 2, x + rr, y + rr // 2], fill=col)
    return img


# ---------------------------------------------------------------------------
# Background: classroom
# ---------------------------------------------------------------------------

def draw_classroom(width=700, height=360):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)

    # wall
    rect(d, 0, 0, width, 250, (216, 200, 168))
    rect(d, 0, 0, width, 30, (186, 168, 138))
    rect(d, 0, 190, width, 250, (122, 138, 114))
    hline(d, 0, width, 190, (96, 112, 92))
    hline(d, 0, width, 192, (150, 164, 130))
    # wall texture
    rnd = random.Random(41)
    for _ in range(160):
        x = rnd.randint(0, width - 1)
        y = rnd.randint(34, 188)
        d.point((x, y), fill=(206, 190, 158) if rnd.random() < 0.5 else (224, 208, 176))

    # classroom door (the protagonist enters from here)
    rect(d, 0, 92, 52, 252, C("brown"))
    rect(d, 4, 96, 48, 250, (154, 122, 92))
    rect(d, 8, 100, 44, 146, (134, 176, 190))
    rect(d, 10, 102, 42, 144, (166, 202, 212))
    rect(d, 10, 102, 42, 108, C("white"))
    rect(d, 8, 150, 44, 246, (138, 108, 82))
    rect(d, 8, 150, 44, 153, (110, 86, 68))
    rect(d, 38, 188, 43, 198, C("gold"))
    rect(d, 0, 92, 52, 96, C("brown_hi"))

    # ceiling lights
    for lx in (120, 380, 620):
        rect(d, lx - 34, 8, lx + 34, 14, C("ink2"))
        rect(d, lx - 32, 9, lx + 32, 13, C("white"))
        rect(d, lx - 30, 10, lx + 30, 12, C("warm"))

    # blackboard
    rect(d, 54, 56, 296, 178, C("ink2"))
    rect(d, 58, 60, 292, 174, C("brown"))
    rect(d, 62, 64, 288, 170, (47, 74, 58))
    rect(d, 62, 64, 288, 67, (58, 90, 70))
    # chalk marks
    for i in range(5):
        y = 80 + i * 17
        d.line([(78, y), (78 + rnd.randint(70, 180), y)], fill=(190, 200, 180), width=1)
    d.line([(210, 76), (210, 118)], fill=(210, 216, 198), width=1)
    d.line([(196, 96), (224, 96)], fill=(210, 216, 198), width=1)
    rect(d, 246, 140, 276, 158, (190, 200, 180))
    # chalk tray
    rect(d, 60, 174, 290, 180, C("brown_mid"))
    rect(d, 84, 174, 104, 178, C("white_sh"))

    # clock
    d.ellipse([168, 28, 196, 56], fill=C("ink2"))
    d.ellipse([170, 30, 194, 54], fill=C("cream"))
    d.line([(182, 42), (182, 34)], fill=C("ink"), width=1)
    d.line([(182, 42), (188, 46)], fill=C("ink"), width=1)

    # notice board
    rect(d, 306, 74, 352, 156, C("brown"))
    rect(d, 309, 77, 349, 153, (188, 170, 140))
    for i in range(4):
        rect(d, 314, 84 + i * 16, 344, 94 + i * 16, C("white"))
        hline(d, 316, 338, 88 + i * 16, C("white_dk"))
        hline(d, 316, 334, 91 + i * 16, C("white_dk"))

    # windows
    win_x0, win_x1 = 368, 692
    rect(d, win_x0 - 6, 48, win_x1 + 4, 226, C("brown"))
    rect(d, win_x0, 52, win_x1, 222, C("ink2"))
    # sky panes
    for px in range(win_x0 + 4, win_x1, 82):
        rect(d, px, 56, min(px + 78, win_x1), 218, (164, 206, 224))
        # distant trees
        for _ in range(12):
            tx = rnd.randint(px + 2, min(px + 76, win_x1 - 1))
            ty = rnd.randint(150, 210)
            rr = rnd.randint(4, 11)
            d.ellipse([tx - rr, ty - rr, tx + rr, ty + rr // 2],
                      fill=rnd.choice([C("green_mid"), C("green_hi"), C("green")]))
        # clouds
        rect(d, px + 8, 72, px + 52, 78, C("white"))
        rect(d, px + 16, 66, px + 44, 72, C("white"))
        # mullions
        vline(d, px - 2, 52, 222, C("brown"))
        hline(d, px, min(px + 78, win_x1), 138, C("brown"))
        vline(d, px + 38, 52, 222, C("brown"))
    # windowsill
    rect(d, win_x0 - 10, 222, win_x1 + 8, 230, C("brown_hi"))
    rect(d, win_x0 - 8, 230, win_x1 + 6, 234, C("brown_mid"))

    # curtains
    for cx, flip in ((win_x0 + 6, 1), (win_x1 - 30, -1)):
        rect(d, cx, 52, cx + 24, 230, (226, 213, 176))
        for i in range(4):
            vline(d, cx + 3 + i * 6, 52, 230, (204, 190, 154))
        rect(d, cx, 52, cx + 24, 56, C("brown_mid"))

    # sunlight beams
    beam = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beam)
    bd.polygon([(430, 60), (500, 60), (640, 330), (500, 330)], fill=(255, 226, 160, 38))
    bd.polygon([(552, 60), (596, 60), (690, 330), (610, 330)], fill=(255, 226, 160, 28))
    img.alpha_composite(beam)

    # floor
    rect(d, 0, 250, width, 360, (160, 132, 106))
    rect(d, 0, 250, width, 262, (138, 111, 90))
    for i in range(10):
        y = 262 + i * 10
        hline(d, 0, width, y, (146, 119, 96))
    # plank seams
    for i in range(16):
        x = i * 46
        d.line([(x, 250), (x - 40, 360)], fill=(145, 118, 95), width=1)
    # scuffs
    for _ in range(30):
        x = rnd.randint(0, width - 20)
        y = rnd.randint(266, 354)
        rect(d, x, y, x + rnd.randint(4, 12), y + 1, (150, 122, 98))

    # back desks / chairs
    def desk(x, y, w=86, h=18, top=True):
        # chair behind
        rect(d, x + 16, y - 16, x + w - 18, y - 10, C("brown"))
        rect(d, x + 18, y - 10, x + w - 20, y - 2, C("brown_mid"))
        rect(d, x + 22, y - 2, x + 26, y + 16, C("ink2"))
        rect(d, x + w - 28, y - 2, x + w - 24, y + 16, C("ink2"))
        # desk top
        rect(d, x, y, x + w, y + 6, C("brown_hi"))
        rect(d, x, y, x + w, y + 2, C("brown_mid"))
        # desk front
        rect(d, x + 4, y + 6, x + w - 4, y + 20, C("brown"))
        rect(d, x + 8, y + 8, x + w - 8, y + 18, C("brown_mid"))
        # legs
        rect(d, x + 6, y + 20, x + 10, y + 36, C("ink2"))
        rect(d, x + w - 14, y + 20, x + w - 10, y + 36, C("ink2"))
        # book on desk
        rect(d, x + 14, y - 3, x + 34, y + 1, C("red_soft"))
        rect(d, x + 16, y - 4, x + 36, y, C("white"))

    for i, x in enumerate((96, 236, 376, 516)):
        desk(x, 286, 96, 18)
    # back row smaller
    for i, x in enumerate((150, 300, 450, 590)):
        desk(x, 262, 78, 14)

    return img


def draw_classroom_fg(width=700, height=360):
    """Near desk row drawn over the seated characters."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def desk_front(x, y, w=104):
        # desk top lip
        rect(d, x, y, x + w, y + 5, C("brown_hi"))
        rect(d, x, y, x + w, y + 2, C("brown_mid"))
        # front panel
        rect(d, x + 4, y + 5, x + w - 4, y + 30, C("brown"))
        rect(d, x + 8, y + 8, x + w - 8, y + 27, C("brown_mid"))
        rect(d, x + 12, y + 10, x + w - 12, y + 12, C("brown_hi"))
        # legs
        rect(d, x + 7, y + 30, x + 12, y + 58, C("ink2"))
        rect(d, x + w - 15, y + 30, x + w - 10, y + 58, C("ink2"))
        # chair back bottom
        rect(d, x + 24, y + 42, x + w - 24, y + 46, C("brown"))

    for x in (116, 268, 420, 572):
        desk_front(x, 298, 110)
    # very near foreground desk corner
    rect(d, -20, 338, 160, 360, C("ink2"))
    rect(d, -18, 334, 158, 346, C("brown_mid"))
    return img


# ---------------------------------------------------------------------------
# Background: bedrooms
# ---------------------------------------------------------------------------

def draw_bedroom(width=320, height=360, female=False):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    rnd = random.Random(71 if female else 61)

    wall = (62, 50, 78) if female else (40, 48, 74)
    wall2 = (76, 60, 92) if female else (50, 59, 88)
    floor = (72, 56, 70) if female else (48, 44, 62)

    rect(d, 0, 0, width, 300, wall)
    rect(d, 0, 0, width, 22, (26, 25, 42))
    for x in range(0, width, 32):
        vline(d, x, 22, 300, wall2)
    for _ in range(70):
        x = rnd.randint(0, width - 1)
        y = rnd.randint(24, 296)
        d.point((x, y), fill=wall2 if rnd.random() < 0.5 else wall)
    rect(d, 0, 300, width, height, floor)
    hline(d, 0, width, 300, (34, 30, 46))
    # skirting board
    rect(d, 0, 294, width, 300, (30, 28, 44))

    # window
    wx0, wx1 = (16, 124) if female else (196, 304)
    rect(d, wx0 - 5, 44, wx1 + 5, 186, C("ink2"))
    rect(d, wx0, 48, wx1, 182, C("night"))
    dither_gradient(img, wx0 + 2, 50, wx1 - 2, 180, [
        (0.00, C("night")),
        (0.55, (36, 46, 78)),
        (1.00, (58, 64, 98)),
    ], scale=2)
    # moon (soft halo is composited by the player, not baked in)
    mx = (wx0 + wx1) // 2
    d.ellipse([mx - 11, 72, mx + 11, 94], fill=C("cream"))
    d.ellipse([mx - 8, 74, mx + 8, 92], fill=(248, 238, 210))
    d.point((mx + 5, 80), fill=(226, 218, 190))
    d.point((mx - 4, 88), fill=(226, 218, 190))
    # rain
    for _ in range(46):
        rx = rnd.randint(wx0 + 3, wx1 - 4)
        ry = rnd.randint(52, 176)
        d.line([(rx, ry), (rx - 2, ry + 5)], fill=(96, 126, 158), width=1)
    # window frame
    vline(d, (wx0 + wx1) // 2, 48, 182, C("brown"))
    hline(d, wx0, wx1, 114, C("brown"))
    rect(d, wx0 - 8, 182, wx1 + 8, 188, C("brown_hi"))
    # curtain
    cx = wx0 - 12 if female else wx1 - 8
    rect(d, cx, 46, cx + 20, 194, (86, 74, 92) if female else (58, 62, 88))
    for i in range(3):
        vline(d, cx + 4 + i * 6, 46, 194, (70, 60, 78) if female else (46, 50, 74))

    # wall decor
    if female:
        for i in range(3):
            px = 150 + i * 34
            rect(d, px, 70, px + 24, 94, C("brown"))
            rect(d, px + 2, 72, px + 22, 92, C("cream"))
            rect(d, px + 5, 76, px + 19, 88, (150, 130, 120))
            d.point((px + 12, 74), fill=C("red"))
        for i in range(6):
            x = 146 + i * 22
            y = 58 + (i % 2) * 4
            d.point((x, y), fill=C("warm"))
    else:
        rect(d, 34, 74, 122, 80, C("brown"))
        rect(d, 38, 62, 48, 74, C("red"))
        rect(d, 52, 64, 62, 74, C("cream"))
        rect(d, 66, 60, 76, 74, C("cool_mid"))
        rect(d, 80, 66, 90, 74, C("white_sh"))
        rect(d, 42, 108, 114, 166, C("ink2"))
        rect(d, 45, 111, 111, 163, (54, 72, 98))
        rect(d, 50, 118, 106, 120, C("cool"))
        rect(d, 50, 126, 88, 128, C("cool_mid"))
        d.ellipse([68, 138, 92, 158], fill=C("warm_mid"))

    # desk, on the window side
    if female:
        dx0, dx1 = 8, 124
    else:
        dx0, dx1 = 196, 312
    rect(d, dx0, 196, dx1, 204, C("brown_hi"))
    rect(d, dx0 + 4, 204, dx1 - 4, 258, C("brown"))
    rect(d, dx0 + 8, 208, dx1 - 8, 254, C("brown_mid"))
    rect(d, dx0 + 6, 258, dx0 + 12, 306, C("ink2"))
    rect(d, dx1 - 12, 258, dx1 - 6, 306, C("ink2"))
    # books
    rect(d, dx0 + 10, 186, dx0 + 34, 196, C("red"))
    rect(d, dx0 + 12, 190, dx0 + 32, 196, C("white"))
    rect(d, dx0 + 36, 188, dx0 + 52, 196, C("cool_mid"))
    # lamp
    lx = dx1 - 26
    rect(d, lx, 178, lx + 14, 196, C("ink2"))
    rect(d, lx + 2, 180, lx + 12, 188, C("warm"))
    rect(d, lx + 5, 188, lx + 9, 198, C("brown"))
    # small personal item
    if female:
        rect(d, dx0 + 60, 180, dx0 + 68, 186, C("red"))
    else:
        rect(d, dx0 + 60, 182, dx0 + 70, 188, C("ink2"))

    # bed
    bx0, bx1 = (130, 312) if female else (8, 190)
    rect(d, bx0, 214, bx1, 232, C("brown_hi"))       # headboard
    rect(d, bx0, 232, bx1, 344, C("brown"))          # frame
    rect(d, bx0 + 4, 244, bx1 - 4, 340, (94, 72, 82) if female else (58, 58, 78))
    rect(d, bx0 + 2, 246, bx1 - 2, 334, C("cream"))  # mattress
    bcol = C("rose") if female else (58, 88, 108)
    bcol2 = (150, 94, 108) if female else (78, 112, 132)
    # blanket base (the foreground fold is a separate layer)
    rect(d, bx0 + 2, 276, bx1 - 2, 340, bcol)
    rect(d, bx0 + 2, 276, bx1 - 2, 282, bcol2)
    for i in range(5):
        hline(d, bx0 + 8, bx1 - 8, 290 + i * 9, (bcol[0] - 12, bcol[1] - 12, bcol[2] - 10))
    # pillow
    rect(d, bx0 + 8, 236, bx0 + 78, 270, C("white_sh"))
    rect(d, bx0 + 10, 238, bx0 + 76, 266, C("cream"))
    rect(d, bx0 + 12, 240, bx0 + 74, 244, C("white"))
    # bed legs
    rect(d, bx0 + 2, 344, bx0 + 10, 352, C("ink2"))
    rect(d, bx1 - 10, 344, bx1 - 2, 352, C("ink2"))

    # rug
    if female:
        rect(d, 138, 312, 300, 344, (86, 62, 78))
    else:
        rect(d, 26, 312, 172, 344, (54, 62, 80))
    for i in range(3):
        hline(d, (138 if female else 26) + 8, (300 if female else 172) - 8,
              318 + i * 8, (70, 52, 66) if female else (44, 52, 70))

    return img


def draw_bedroom_fg(width=320, height=360, female=False):
    """Blanket fold drawn over the seated character's lap."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bx0, bx1 = (130, 312) if female else (8, 190)
    bcol = C("rose") if female else (58, 88, 108)
    bcol2 = (150, 94, 108) if female else (78, 112, 132)
    dark = (bcol[0] - 14, bcol[1] - 14, bcol[2] - 12)

    # wavy top edge
    for x in range(bx0 + 2, bx1 - 2):
        wave = 2 if ((x - bx0) // 18) % 2 == 0 else 0
        y = 300 + wave
        vline(d, x, y, 344, bcol)
    # top highlight
    for x in range(bx0 + 2, bx1 - 2):
        wave = 2 if ((x - bx0) // 18) % 2 == 0 else 0
        d.point((x, 300 + wave), fill=bcol2)
    # folds
    for i in range(5):
        fx = bx0 + 18 + i * ((bx1 - bx0) // 5)
        vline(d, fx, 304, 342, dark)
        d.point((fx + 1, 304), fill=bcol2)
    # bottom seam
    hline(d, bx0 + 2, bx1 - 2, 342, dark)
    return img


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------

def build_glow(name, size, color, steps=(0.05, 0.10, 0.16, 0.24, 0.34, 0.46)):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = size // 2
    maxr = size // 2
    for rr, alpha in zip([int(maxr * s) for s in reversed(steps)], range(10, 10 + len(steps) * 8, 8)):
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(color[0], color[1], color[2], alpha))
    img.save(os.path.join(FX, name))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ensure_dirs():
    for p in (SPRITES, BG, FX):
        os.makedirs(p, exist_ok=True)


def main():
    ensure_dirs()

    manifests = {}
    print("building sprites...")
    manifests["male"] = build_sprite_sheet("male", female=False)
    manifests["female"] = build_sprite_sheet("female", female=True)
    with open(os.path.join(SPRITES, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"cell": [CELL_W, CELL_H], "anchor": [CX, FEET], "characters": manifests},
                  f, ensure_ascii=False, indent=2)

    print("building backgrounds...")
    gate = draw_gate()
    gate.save(os.path.join(BG, "gate_dusk.png"))
    draw_gate_fg().save(os.path.join(BG, "gate_dusk_fg.png"))

    draw_classroom().save(os.path.join(BG, "classroom_day.png"))
    draw_classroom_fg().save(os.path.join(BG, "classroom_day_fg.png"))

    draw_bedroom(female=False).save(os.path.join(BG, "bedroom_male_night.png"))
    draw_bedroom(female=True).save(os.path.join(BG, "bedroom_female_night.png"))
    draw_bedroom_fg(female=False).save(os.path.join(BG, "bedroom_male_fg.png"))
    draw_bedroom_fg(female=True).save(os.path.join(BG, "bedroom_female_fg.png"))

    print("building fx...")
    build_glow("glow_blue.png", 96, (120, 190, 255))
    build_glow("glow_warm.png", 96, (255, 210, 150))
    build_glow("glow_moon.png", 96, (240, 232, 200))

    print("done")


if __name__ == "__main__":
    main()
