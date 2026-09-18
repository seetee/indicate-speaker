"""The overlay: a backdrop panel with one avatar per player.

- whose view we watch: that avatar is enlarged and gets a name label in the
  player's colour; on a cut the label slides over, blending colour, and the
  avatars re-size;
- who is talking: a ring in the player's colour plus a small badge with sound
  bars (so it never relies on colour alone), faded in and out;
- when no player's view is on screen (intro, outro, full-screen assets) the
  whole overlay fades out.

The canvas has the profile's aspect ratio, so MLT maps it 1:1 onto a
qtblend rect of the same size; no scaling maths in Kdenlive.
"""

import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import skia

from .config import Theme, die

FADE = 0.30       # s; overlay in/out and the cut transition
TALK_FADE = 0.08  # s; talking marker in/out
DIM = 0.6         # opacity of avatars that are neither viewed nor talking
BLACK, WHITE = (0, 0, 0), (255, 255, 255)

# theme `codec` -> (file extension, container, pixel format, encoder options).
# Both are lossless with alpha and decode in Kdenlive; never add a codec
# without checking that its alpha survives there (VP8/VP9 WebM alpha does not).
CODECS = {
    "ffv1": (".mkv", "matroska", "bgra",
             {"level": "3", "slices": "4", "slicecrc": "1", "context": "1"}),
    "qtrle": (".mov", "mov", "argb", {}),
}


def luminance(rgb) -> float:
    c = [v / 255 for v in rgb]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)))
    return (lb + 0.05) / (la + 0.05)


def on_colour(rgb):
    """Black or white, whichever reads better on rgb. The better of the two is
    always >= 4.58:1, so WCAG AA text contrast holds for any player colour."""
    return max((BLACK, WHITE), key=lambda t: contrast(rgb, t))


def sk(rgb, alpha=1.0):
    return skia.Color(*rgb, round(255 * alpha))


@dataclass(frozen=True)
class Layout:
    W: int                 # canvas size
    H: int
    panel: skia.Rect
    S: int                 # avatar size, idle
    V: int                 # avatar size, viewed
    slots: tuple[tuple[float, float], ...]    # avatar centre per player
    labels: tuple[tuple[float, float], ...]   # name label anchor per player: (x, top)
    align: str             # label x anchor is its "center", "left" or "right" edge
    label_h: float
    font_size: int


def load_font(theme: Theme, size: int) -> skia.Font:
    if theme.font:
        face = skia.Typeface.MakeFromFile(str(theme.font))
        if face is None:
            die(f"font {theme.font} is not a font file skia can read (use .ttf or .otf)")
    else:
        face = skia.Typeface("sans-serif", skia.FontStyle.Bold())
    return skia.Font(face, size)


def load_image(path: Path, what: str) -> skia.Image:
    try:
        return skia.Image.open(str(path))
    except RuntimeError:
        die(f"{what} {path} is not an image skia can read (use .png, .jpg or .webp)")


def layout(theme: Theme, frame_w: int, frame_h: int) -> Layout:
    """Horizontal: a row of avatars, the name label under the viewed one.
    Vertical: a column of avatars along the frame edge, the label beside the
    viewed one, pointing into the picture."""
    n, S = len(theme.players), theme.avatar_size
    V = round(S * 1.25)
    gap, pad = max(4, S // 4), max(6, S // 4)
    font_size = max(11, round(S / 3))
    label_h = round(font_size * 1.7)
    right = theme.position.endswith("right")
    run = [pad + V / 2 + i * (V + gap) for i in range(n)]   # centres along the bar
    font = load_font(theme, font_size)
    label_w = max(font.measureText(p.name) for p in theme.players) + label_h
    row = 2 * pad + n * V + (n - 1) * gap
    if theme.orientation == "vertical":
        pw = pad + V + gap + label_w + pad
        ph = row
    else:
        pw = max(row, label_w + 8)       # the label is kept 4 px inside the panel
        ph = pad + V + gap // 2 + label_h + pad
        run = [x + (pw - row) / 2 for x in run]   # centre the avatars in a wider panel
    g = math.gcd(frame_w, frame_h)
    aw, ah = frame_w // g, frame_h // g
    k = math.ceil(max(pw / aw, ph / ah))
    W, H = aw * k, ah * k
    px = W - pw if right else 0
    py = H - ph if theme.position.startswith("bottom") else 0
    if theme.orientation == "vertical":
        cx = px + pw - pad - V / 2 if right else px + pad + V / 2
        slots = tuple((cx, py + y) for y in run)
        edge = cx - V / 2 - gap if right else cx + V / 2 + gap
        labels = tuple((edge, y - label_h / 2) for _, y in slots)
        align = "right" if right else "left"
    else:
        slots = tuple((px + x, py + pad + V / 2) for x in run)
        labels = tuple((x, py + pad + V + gap // 2) for x, _ in slots)
        align = "center"
    return Layout(W, H, skia.Rect.MakeXYWH(px, py, pw, ph), S, V, slots, labels, align,
                  label_h, font_size)


def placement(theme: Theme, lay: Layout, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """Where the canvas goes in the frame: (x, y, w, h) for the qtblend rect."""
    m = theme.margin
    x = frame_w - m - lay.W if theme.position.endswith("right") else m
    y = frame_h - m - lay.H if theme.position.startswith("bottom") else m
    return x, y, lay.W, lay.H


def ramp(target: np.ndarray, frames: float) -> np.ndarray:
    """0/1 targets -> values that move towards them by 1/frames per frame."""
    step = 1 / max(frames, 1)
    out = np.empty(len(target), np.float32)
    v = 0.0
    for i, t in enumerate(target):
        v = min(v + step, 1.0) if t else max(v - step, 0.0)
        out[i] = v
    return out


def ease(t: float) -> float:
    return 1 - (1 - t) ** 3


class Painter:
    def __init__(self, theme: Theme, lay: Layout):
        self.theme, self.lay = theme, lay
        self.surface = skia.Surface(lay.W, lay.H)
        self.avatars = [load_image(p.avatar, f"{p.name}'s avatar") for p in theme.players]
        self.backdrop = load_image(theme.backdrop, "backdrop") if theme.backdrop else None
        self.font = load_font(theme, lay.font_size)

    def _image(self, c: skia.Canvas, img: skia.Image, dst: skia.Rect, cover=False):
        if cover:   # crop to fill dst without distortion
            s = max(dst.width() / img.width(), dst.height() / img.height())
            w, h = dst.width() / s, dst.height() / s
            src = skia.Rect.MakeXYWH((img.width() - w) / 2, (img.height() - h) / 2, w, h)
        else:
            src = skia.Rect.MakeWH(img.width(), img.height())
        # pixel art (Minecraft heads) stays crisp when enlarged
        sampling = (skia.SamplingOptions(skia.FilterMode.kNearest)
                    if img.width() < dst.width() else
                    skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear))
        c.drawImageRect(img, src, dst, sampling)

    def draw(self, vis: float, a: int, b: int, t: float, talk: tuple[float, ...]) -> np.ndarray:
        """a -> b: previous and current viewed player, t: 0..1 through the cut."""
        lay, c = self.lay, self.surface.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        if vis > 0 and b >= 0:
            c.saveLayerAlpha(None, round(255 * vis))
            self._panel(c)
            e = ease(t) if a >= 0 else 1.0
            for i, p in enumerate(self.theme.players):
                w = e if i == b else (1 - e) if i == a else 0.0
                self._avatar(c, i, lay.S + (lay.V - lay.S) * w,
                             DIM + (1 - DIM) * max(w, talk[i]), talk[i])
            self._label(c, a if a >= 0 else b, b, e)
            c.restore()
        return self.surface.makeImageSnapshot().toarray(
            colorType=skia.kBGRA_8888_ColorType, alphaType=skia.kUnpremul_AlphaType)

    def _panel(self, c):
        rr = skia.RRect.MakeRectXY(self.lay.panel, self.lay.S / 4, self.lay.S / 4)
        c.save()
        c.clipRRect(rr, doAntiAlias=True)
        if self.backdrop:
            self._image(c, self.backdrop, self.lay.panel, cover=True)
        c.drawPaint(skia.Paint(Color=sk(BLACK, 0.35 if self.backdrop else 0.6)))  # scrim
        c.restore()

    def _avatar(self, c, i, size, alpha, talk):
        lay, colour = self.lay, self.theme.players[i].colour
        cx, cy = lay.slots[i]
        box = skia.Rect.MakeXYWH(cx - size / 2, cy - size / 2, size, size)
        r = size * 0.18
        c.saveLayerAlpha(None, round(255 * alpha))
        c.save()
        c.clipRRect(skia.RRect.MakeRectXY(box, r, r), doAntiAlias=True)
        self._image(c, self.avatars[i], box)
        c.restore()
        if talk > 0:
            ring = box.makeOutset(4, 4)
            rr = skia.RRect.MakeRectXY(ring, r + 4, r + 4)
            for rgb, width in ((on_colour(colour), 5), (colour, 3)):   # edge keeps it visible on any backdrop
                c.drawRRect(rr, skia.Paint(Color=sk(rgb, talk), AntiAlias=True,
                                           Style=skia.Paint.kStroke_Style, StrokeWidth=width))
            self._badge(c, box.right() - 2, box.top() + 2, colour, talk)
        c.restore()

    def _badge(self, c, x, y, colour, alpha):
        rad = max(6, self.lay.S / 6)
        c.drawCircle(x, y, rad + 1, skia.Paint(Color=sk(on_colour(colour), alpha), AntiAlias=True))
        c.drawCircle(x, y, rad, skia.Paint(Color=sk(colour, alpha), AntiAlias=True))
        bar = skia.Paint(Color=sk(on_colour(colour), alpha), AntiAlias=True)
        bw = rad / 3.5
        for k, h in enumerate((0.5, 1.0, 0.7)):
            bx = x + (k - 1) * bw * 1.6 - bw / 2
            bh = rad * 1.1 * h
            c.drawRoundRect(skia.Rect.MakeXYWH(bx, y - bh / 2, bw, bh), bw / 2, bw / 2, bar)

    def _label(self, c, a, b, e):
        """One name pill sliding from player a's slot to b's, blending colour;
        the text flips half-way and is re-contrasted for every blended colour."""
        lay, pa, pb = self.lay, self.theme.players[a], self.theme.players[b]
        colour = tuple(round(x + (y - x) * e) for x, y in zip(pa.colour, pb.colour))
        name = pb.name if e >= 0.5 else pa.name
        wa, wb = (self.font.measureText(p.name) + lay.label_h for p in (pa, pb))
        w = wa + (wb - wa) * e
        (ax, ay), (bx, by) = lay.labels[a], lay.labels[b]
        anchor, top = ax + (bx - ax) * e, ay + (by - ay) * e
        x = {"center": anchor - w / 2, "left": anchor, "right": anchor - w}[lay.align]
        x = min(max(x, lay.panel.left() + 4), lay.panel.right() - 4 - w)
        box = skia.Rect.MakeXYWH(x, top, w, lay.label_h)
        c.drawRRect(skia.RRect.MakeRectXY(box, lay.label_h / 2, lay.label_h / 2),
                    skia.Paint(Color=sk(colour), AntiAlias=True))
        m = self.font.getMetrics()
        base = top + (lay.label_h - (m.fDescent - m.fAscent)) / 2 - m.fAscent
        c.drawString(name, x + (w - self.font.measureText(name)) / 2, base, self.font,
                     skia.Paint(Color=sk(on_colour(colour)), AntiAlias=True))


def frame_states(spans, talking: list[np.ndarray], fps: float, n: int):
    """Per frame: (visibility, previous viewer, viewer, transition 0..1, talk levels)."""
    shown = np.full(n, -1, np.int16)
    before = np.full(n, -1, np.int16)
    since = np.zeros(n, np.int64)
    visible = np.zeros(n, bool)
    last, prev, switch = -1, -1, 0
    for s, e, who in spans:
        if s >= n:
            break
        e = min(e, n)
        if who >= 0 and who != last:
            prev, last, switch = last, who, s
        shown[s:e], before[s:e] = last, prev
        since[s:e] = np.arange(s, e) - switch
        visible[s:e] = who >= 0
    vis = ramp(visible, FADE * fps)
    levels = [ramp(t[:n], TALK_FADE * fps) for t in talking]
    for f in range(n):
        yield (round(float(vis[f]), 3), int(before[f]), int(shown[f]),
               round(min(since[f] / (FADE * fps), 1.0), 3),
               tuple(round(float(l[f]), 2) for l in levels))


def render(painter: Painter, codec: str, spans, talking, fps: float, n: int, out: Path,
           progress=lambda done, total: None) -> None:
    """Encode n frames to `out` (its extension must match the codec's)."""
    _, container, pix_fmt, options = CODECS[codec]
    rate = Fraction(fps).limit_denominator(1001)
    tmp = out.with_name(out.name + ".partial")
    cache: dict = {}
    try:
        with av.open(str(tmp), "w", format=container) as box:
            st = box.add_stream(codec, rate=rate)
            st.width, st.height, st.pix_fmt = painter.lay.W, painter.lay.H, pix_fmt
            st.options = options
            for f, state in enumerate(frame_states(spans, talking, fps, n)):
                img = cache.get(state)
                if img is None:
                    if len(cache) > 256:   # steady states repeat; ramps are short-lived
                        cache.clear()
                    img = cache[state] = painter.draw(*state)
                frame = av.VideoFrame.from_ndarray(img, format="bgra")
                if pix_fmt != "bgra":
                    frame = frame.reformat(format=pix_fmt)
                frame.pts, frame.time_base = f, 1 / rate
                box.mux(st.encode(frame))
                if f % 600 == 0:
                    progress(f, n)
            box.mux(st.encode(None))
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)    # no half-written overlay left behind
        raise
    progress(n, n)
