#!/usr/bin/env python3
#
# indicate-speaker.py — per-voice "who is speaking" overlays for let's-play editing.
# Copyright (C) 2026  seetee
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
indicate-speaker.py — per-voice "who is speaking" overlays for let's-play editing.

For each person in the config, the script:
  1. reads that person's clean voice stream from their MKV (matched by the
     stream *title*, e.g. "Voice audio"),
  2. measures a per-frame loudness envelope and turns it into a smooth
     0..1 "speaking" activation (fast attack, slow release, noise-gated),
  3. renders a transparent overlay video in which that person's Minecraft
     head sits in a fixed upper-left slot and reacts to the activation: it
     scales up, brightens, gains a coloured ring and glow while they talk,
     and sits dim and slightly smaller while they are quiet.

You get one overlay file per person. Each is the exact length of that
person's source recording and starts at the same instant, so you align it
the same way you already align the source files (it even carries a copy of
the voice for waveform sync). Park them on tracks above the views and, if
you like, drop them into a Sequence so they become one tidy, still-cuttable
object that never moves.

Output is QuickTime Animation (qtrle) in a .mov: lossless alpha that
Kdenlive decodes reliably. (VP9/VP8 alpha WebM is smaller but its alpha is
dropped on decode by the same FFmpeg backend Kdenlive uses, so it is not
offered — it would import as a black box.)

Two canvas modes:
  --canvas tight  (default) just the head sprite. ~24x faster and ~3x
                  smaller; you set each clip's size + position once in
                  Kdenlive (the script prints the exact X/Y) and save it as
                  an effect favourite.
  --canvas full   a 1920x1080 frame with the head pre-positioned. Drop
                  straight onto a track, no positioning, but much bigger
                  (~3 GB/hour) and slower to encode.

Requirements: Python 3.11+, numpy, Pillow, ffmpeg + ffprobe on PATH.

    python3 indicate-speaker.py [CONFIG.toml] [options]

Run it on one episode first. Licensed under the GNU AGPL v3 or later (see
the notice above and the LICENSE file); comes with NO WARRANTY.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

__version__ = "1.0"

ANALYSIS_RATE = 8000      # Hz, mono; ample for loudness, cheap to decode
LEVELS = 64               # quantised activation steps -> pre-rendered sprites
HEAD_FETCH_PX = 256       # size to fetch from the skin API before downscaling
MAX_HEAD_BYTES = 4 << 20  # refuse skin-API responses larger than this

_BLOOM_LAYERS = (          # (radius_factor, blur_factor, alpha_factor)
    (0.52, 0.06, 0.90),    # tight bright core
    (0.68, 0.16, 0.55),    # mid halo
    (0.85, 0.32, 0.28),    # wide soft ambient
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class VoiceError(Exception):
    """Any expected, user-facing failure. Caught and printed in main()."""


def die(msg: str) -> NoReturn:
    raise VoiceError(msg)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        die(f"bad colour '{value}', expected #rrggbb")
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        die(f"bad colour '{value}', expected #rrggbb")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


class Progress:
    """Throttled progress line. live=True overwrites one line with \\r;
    otherwise (parallel jobs or non-terminal) it prints periodic lines."""

    def __init__(self, total: float, label: str, live: bool):
        self.total = max(1.0, float(total))
        self.label = label
        self.live = live
        self.start = time.monotonic()
        self.last = 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def update(self, done: float, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < (0.5 if self.live else 12.0):
            return
        self.last = now
        frac = min(1.0, done / self.total)
        el = now - self.start
        eta = el / frac - el if frac > 0.02 else 0.0
        line = (f"{self.label}  {int(frac * 100):3d}%   "
                f"{_fmt_dur(el)} elapsed   ~{_fmt_dur(eta)} left")
        if self.live:
            print("\r" + line + "   ", end="", flush=True)
        else:
            print(line, flush=True)

    def finish(self, line: str) -> None:
        if self.live:
            print("\r" + line + " " * 24, flush=True)
        else:
            print(line, flush=True)


_warn_lock = threading.Lock()
_warnings: list[str] = []


def warn(msg: str) -> None:
    """Print a warning now and remember it for the end-of-run summary
    (with parallel jobs, warnings otherwise drown between progress lines)."""
    with _warn_lock:
        _warnings.append(msg)
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Layout:
    width: int = 1920
    height: int = 1080
    fps: float = 60.0
    margin_top: int = 12
    margin_left: int = 12
    head_size: int = 56         # pixel size at 100% (speaking)
    gap: int = 8                # vertical gap between heads
    silent_scale: float = 0.88  # head scale when fully quiet
    silent_dim: float = 0.78    # head brightness/alpha when fully quiet
    glow_strength: float = 0.55  # peak glow opacity (scales all bloom layers)
    ring_width: int = 3
    breath_freq: float = 0.40   # Hz; idle breathing pulse rate
    breath_scale: float = 0.06  # ±fraction brightness modulation while silent

    @property
    def sprite(self) -> int:
        # extra room for the multi-layer bloom
        return self.head_size + 2 * max(self.ring_width + 2,
                                        round(self.head_size * 0.42))

    def cell_origin(self, index: int) -> tuple[int, int]:
        """Top-left pixel of person `index`'s sprite on the full canvas."""
        cx = self.margin_left + self.head_size / 2
        cy = (self.margin_top
              + index * (self.head_size + self.gap)
              + self.head_size / 2)
        return (int(round(cx - self.sprite / 2)),
                int(round(cy - self.sprite / 2)))


@dataclass
class Gate:
    open_db: float = -38.0      # envelope reaches >0 above this
    full_db: float = -16.0      # envelope reaches 1.0 at/above this
    close_db: float = -46.0     # below this, forced toward silence
    spring_stiffness: float = 400.0     # k; higher = faster snap to speaking
    spring_damping_ratio: float = 0.65  # ζ; 1.0 = no overshoot, lower = bouncier
    normalize: bool = False     # derive open/full thresholds per-person
    norm_low_pct: float = 15.0  # percentile of active frames → open_db
    norm_high_pct: float = 90.0 # percentile of active frames → full_db


@dataclass
class Person:
    name: str
    colour: tuple[int, int, int]
    source: Path | None = None   # explicit file; else resolved from suffix
    suffix: str | None = None    # the k/h/r/j in YYYY-MM-DD_k.mkv
    nick: str | None = None
    head_file: Path | None = None
    stream_title: str = "Voice audio"
    gate: Gate | None = None     # the global [gate] plus this person's overrides


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def ffprobe_streams(path: Path) -> list[dict]:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", str(path)])
    if out.returncode != 0:
        die(f"ffprobe failed on {path}:\n{out.stderr}")
    return json.loads(out.stdout).get("streams", [])


def find_audio_index(path: Path, title: str) -> int:
    """Absolute stream index of the audio stream whose title matches."""
    audio = [s for s in ffprobe_streams(path)
             if s.get("codec_type") == "audio"]
    if not audio:
        die(f"{path} has no audio streams")
    for s in audio:
        if (s.get("tags", {}).get("title", "").strip().lower()
                == title.strip().lower()):
            return int(s["index"])
    titles = [s.get("tags", {}).get("title", "<untitled>") for s in audio]
    die(f"{path}: no audio stream titled '{title}'. "
        f"Found: {titles}. Set stream_title in the config to match, "
        f"or run --discover to pick interactively.")


def media_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    if out.returncode != 0 or not out.stdout.strip():
        die(f"could not read duration of {path}")
    return float(out.stdout.strip())


def frame_loudness_db(path: Path, stream_index: int, fps: float, n_frames: int,
                      progress_cb: Callable[[float], None] | None = None,
                      voice_out: Path | None = None,
                      limit_s: float | None = None,
                      abort: threading.Event | None = None) -> np.ndarray:
    """Per-video-frame RMS loudness (dBFS) of one audio stream.

    Reads the source exactly once. While decoding for loudness it can, in the
    same pass, write the voice stream to `voice_out` (a small AAC file) so the
    caller can mux it into the overlay without re-reading the big source.
    """
    sumsq = np.zeros(n_frames, dtype=np.float64)
    count = np.zeros(n_frames, dtype=np.int64)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    if limit_s is not None:
        cmd += ["-t", f"{limit_s}"]
    cmd += ["-i", str(path),
            "-map", f"0:{stream_index}", "-ac", "1",
            "-ar", str(ANALYSIS_RATE), "-f", "s16le", "pipe:1"]
    if voice_out is not None:
        cmd += ["-map", f"0:{stream_index}", "-ac", "1",
                "-c:a", "aac", "-b:a", "128k", str(voice_out)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    if proc.stdout is None:
        die("internal error: ffmpeg stdout not captured")
    sample_pos = 0
    pending = b""
    try:
        while True:
            if abort is not None and abort.is_set():
                raise VoiceError("stopped (another overlay failed)")
            raw = proc.stdout.read(1 << 20)
            if not raw:
                break
            if pending:
                raw = pending + raw
                pending = b""
            if len(raw) & 1:        # pipe reads can split a 16-bit sample
                pending = raw[-1:]
                raw = raw[:-1]
                if not raw:
                    continue
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
            idx = ((np.arange(sample_pos, sample_pos + len(samples)) * fps)
                   / ANALYSIS_RATE).astype(np.int64)
            np.clip(idx, 0, n_frames - 1, out=idx)
            sumsq += np.bincount(idx, weights=samples * samples,
                                 minlength=n_frames)
            count += np.bincount(idx, minlength=n_frames)
            sample_pos += len(samples)
            if progress_cb is not None:
                progress_cb(sample_pos)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.wait() != 0:
        die(f"ffmpeg failed decoding audio from {path}")
    if sample_pos == 0:
        die(f"no audio samples decoded from {path} stream {stream_index}")
    if voice_out is not None and not voice_out.is_file():
        die(f"failed to extract voice track from {path}")
    count[count == 0] = 1
    rms = np.sqrt(sumsq / count) / 32768.0
    return 20.0 * np.log10(rms + 1e-9)


class Envelope(NamedTuple):
    env: np.ndarray      # per-frame 0..1 speaking activation
    open_db: float       # thresholds actually used (post-normalise)
    full_db: float
    close_db: float
    peak_db: float       # 99.5th-percentile loudness of the whole track


def activation_envelope(db: np.ndarray, fps: float, g: Gate) -> Envelope:
    """Map loudness to a smooth 0..1 speaking activation.

    When normalize is on, all three thresholds — including the close/force-
    silent cutoff — are derived from this person's own loudness distribution
    rather than the config values, so a mic recorded 30 dB quieter than the
    rest still animates. (An absolute close_db would zero out such a track
    before the percentile statistics ever saw it.)
    """
    open_db = g.open_db
    full_db = g.full_db
    close_db = g.close_db
    peak_db = float(np.percentile(db, 99.5))

    if g.normalize:
        # Speech has a bounded dynamic range: anything more than ~25 dB below
        # this track's loud speech (the peak) is not speech. Anchoring the
        # speech/silence cutoff to the peak — not the floor — keeps it sane
        # for mics whose noise gate turns silence into -180 dB digital zero.
        # Falls back to the config thresholds when the track has too little
        # contrast (near-empty or constant noise) to trust the statistics.
        floor_db = float(np.percentile(db, 20))
        if peak_db - floor_db > 12.0:
            cut = max(peak_db - 25.0, floor_db + 6.0)
            active = db[db > cut]
            # Require at least one second of detectable audio. If the head
            # reacts to near-silence, raise norm_low_pct in the config.
            if len(active) >= max(64, int(fps)):
                open_db = float(np.percentile(active, g.norm_low_pct))
                full_db = float(np.percentile(active, g.norm_high_pct))
                close_db = min(cut, open_db - 2.0)

    span = max(1e-6, full_db - open_db)
    raw = np.clip((db - open_db) / span, 0.0, 1.0)
    raw[db < close_db] = 0.0

    # Second-order mass-spring-damper via semi-implicit Euler. The gate signal
    # is the rest position; underdamping (ζ < 1) produces a natural overshoot
    # on attack so the head snaps past full size then settles. Output is not
    # clamped above 1.0 here — the render loop clips levels to LEVELS-1, so
    # overshoot simply holds the fully-speaking sprite a few extra frames.
    c  = 2.0 * g.spring_damping_ratio * math.sqrt(g.spring_stiffness)
    dt = 1.0 / fps
    out = np.empty_like(raw)
    x, v = 0.0, 0.0
    for i, target in enumerate(raw):
        v += (g.spring_stiffness * (target - x) - c * v) * dt
        x += v * dt
        out[i] = max(0.0, x)
    return Envelope(out, open_db, full_db, close_db, peak_db)


def speaking_envelope(person: Person, stream_index: int, layout: Layout,
                      gate: Gate, n_frames: int, live: bool,
                      voice_out: Path | None = None,
                      limit_s: float | None = None,
                      abort: threading.Event | None = None,
                      ) -> Envelope:
    dur_s = limit_s if limit_s is not None else n_frames / layout.fps
    ap = Progress(dur_s * ANALYSIS_RATE,
                  f"  {person.name}: analysing audio", live)
    db = frame_loudness_db(person.source, stream_index, layout.fps, n_frames,
                           progress_cb=ap.update, voice_out=voice_out,
                           limit_s=limit_s, abort=abort)
    return activation_envelope(db, layout.fps, gate)


BLEED_WINDOW_S = 300.0     # sampled from the middle of the recording
BLEED_SIG_DB = -60.0       # a sibling stream counts as audible above this
BLEED_MIN_DROP_DB = -8.0   # voice floor this close to the sibling ⇒ bleed


def check_track_bleed(person: Person, aidx: int, duration: float) -> None:
    """Warn when another audio stream leaks into the chosen voice stream.

    Samples a window from the middle of the recording. While a sibling
    stream (game audio, say) is clearly audible, a clean mic track still has
    moments of near-silence far below the sibling's level; if the voice
    envelope never drops meaningfully below the sibling's, the sibling is
    playing inside the voice track — usually OBS routing desktop/game audio
    onto the mic track.
    """
    if duration < 120.0:
        return
    streams = [s for s in ffprobe_streams(person.source)
               if s.get("codec_type") == "audio"]
    if len(streams) < 2:
        return
    win = min(BLEED_WINDOW_S, duration / 2)
    start = duration / 2 - win / 2

    def env_db(index: int) -> np.ndarray | None:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{win:.3f}",
             "-i", str(person.source), "-map", f"0:{index}", "-ac", "1",
             "-ar", str(ANALYSIS_RATE), "-f", "s16le", "pipe:1"],
            capture_output=True)
        if out.returncode != 0:
            return None
        a = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float64)
        w = ANALYSIS_RATE // 10                # 100 ms frames
        n = len(a) // w
        if n < 300:                            # need at least 30 s
            return None
        rms = np.sqrt((a[:n * w].reshape(n, w) ** 2).mean(axis=1))
        return 20.0 * np.log10(rms / 32768.0 + 1e-9)

    voice = env_db(aidx)
    if voice is None:
        return
    for s in streams:
        idx = int(s["index"])
        if idx == aidx:
            continue
        other = env_db(idx)
        if other is None:
            continue
        n = min(len(voice), len(other))
        v, o = voice[:n], other[:n]
        sig = o > BLEED_SIG_DB
        if sig.sum() < 100:                    # sibling mostly silent here;
            continue                           # no verdict either way
        drop = float(np.percentile(v[sig] - o[sig], 10))
        if drop > BLEED_MIN_DROP_DB:
            title = s.get("tags", {}).get("title", "<untitled>")
            warn(f"  {person.name}: warning: stream '{title}' appears to play "
                 f"inside the voice track (the voice never drops more than "
                 f"{-drop:.0f} dB below it) — in OBS Advanced Audio "
                 f"Properties, that source is probably also routed onto the "
                 f"voice track.")


# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------

def head_cache_path(nick: str) -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    safe = urllib.parse.quote(nick, safe="")
    return root / "indicate-speaker" / f"{safe}_{HEAD_FETCH_PX}.png"


def load_head(person: Person) -> Image.Image:
    if person.head_file:
        if not person.head_file.is_file():
            die(f"{person.name}: head_file not found: {person.head_file}")
        return Image.open(person.head_file).convert("RGBA")
    if not person.nick:
        die(f"{person.name}: needs either nick or head_file")

    cache = head_cache_path(person.nick)
    if cache.is_file():
        try:
            return Image.open(cache).convert("RGBA")
        except OSError:
            cache.unlink(missing_ok=True)   # corrupt cache entry; refetch

    url = (f"https://mc-heads.net/avatar/"
           f"{urllib.parse.quote(person.nick, safe='')}/{HEAD_FETCH_PX}.png")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "indicate-speaker"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(MAX_HEAD_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        die(f"{person.name}: could not fetch head from {url}: {exc}. "
            f"Provide head_file in the config instead.")
    if len(data) > MAX_HEAD_BYTES:
        die(f"{person.name}: {url} returned more than "
            f"{MAX_HEAD_BYTES >> 20} MB; refusing it")
    try:
        head = Image.open(BytesIO(data)).convert("RGBA")
    except OSError as exc:
        die(f"{person.name}: {url} did not return a usable image ({exc}). "
            f"Provide head_file in the config instead.")
    # cache only after a successful decode, so an API error page is never kept
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cache)
    return head


def render_sprites(person: Person, layout: Layout,
                   head: Image.Image | None = None) -> list[np.ndarray]:
    """Pre-render LEVELS activation states as (H, W, 4) uint8 numpy arrays."""
    s = layout.sprite
    if head is None:
        head = load_head(person)
    base_head = head.resize(
        (layout.head_size, layout.head_size), Image.Resampling.LANCZOS)
    centre = s / 2
    r, gc, b = person.colour
    sprites: list[np.ndarray] = []

    for level in range(LEVELS):
        act = level / (LEVELS - 1)
        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

        # multi-layer bloom: tight bright core → mid halo → wide soft ambient
        if act > 0.01:
            for rad_f, blur_f, alpha_f in _BLOOM_LAYERS:
                layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                rad = layout.head_size * (rad_f + 0.12 * act)
                a = int(255 * layout.glow_strength * alpha_f * act)
                ld.ellipse([centre - rad, centre - rad,
                            centre + rad, centre + rad], fill=(r, gc, b, a))
                layer = layer.filter(
                    ImageFilter.GaussianBlur(layout.head_size * blur_f))
                canvas = Image.alpha_composite(canvas, layer)

        # the head: scaled and dimmed by activation
        scale = layout.silent_scale + (1.0 - layout.silent_scale) * act
        hs = max(1, int(round(layout.head_size * scale)))
        head = base_head.resize((hs, hs), Image.Resampling.LANCZOS)

        dim = layout.silent_dim + (1.0 - layout.silent_dim) * act
        if dim < 0.999:
            head = ImageEnhance.Brightness(head).enhance(dim)
            alpha = head.split()[3].point(lambda p: int(p * dim))
            head.putalpha(alpha)

        canvas.alpha_composite(head, (int(round(centre - hs / 2)),
                                      int(round(centre - hs / 2))))

        # ring: rounded rectangle hugging the head, just outside it
        if act > 0.01:
            ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            half = layout.head_size / 2 + layout.ring_width + 1
            corner = layout.head_size * 0.18
            rd.rounded_rectangle(
                [centre - half, centre - half, centre + half, centre + half],
                radius=corner,
                outline=(r, gc, b, int(255 * act)),
                width=layout.ring_width)
            canvas = Image.alpha_composite(canvas, ring)

        # corner accents: L-shaped brackets just outside the head corners
        if act > 0.01:
            arm   = max(4, round(hs * 0.15))
            thick = max(2, round(layout.head_size * 0.05))
            gap   = layout.ring_width + 3
            ci    = round(centre)
            hh    = hs / 2
            fill  = (r, gc, b, int(255 * act))
            t0, t1 = -(thick // 2), thick - thick // 2
            acc = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            ad  = ImageDraw.Draw(acc)
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                bx = ci + sx * (hh + gap)
                by = ci + sy * (hh + gap)
                # horizontal arm extends inward along x
                hx0, hx1 = sorted([int(bx), int(bx - sx * arm)])
                ad.rectangle([hx0, int(by + t0), hx1, int(by + t1)], fill=fill)
                # vertical arm extends inward along y
                vy0, vy1 = sorted([int(by), int(by - sy * arm)])
                ad.rectangle([int(bx + t0), vy0, int(bx + t1), vy1], fill=fill)
            canvas = Image.alpha_composite(canvas, acc)

        sprites.append(np.array(canvas))
    return sprites


def contact_sheet(people: list[Person], layout: Layout, out: Path) -> None:
    """Render a quiet-vs-speaking preview of every head."""
    sprites_by_person = [render_sprites(p, layout) for p in people]
    s = layout.sprite
    pad = 24
    label_h = 22
    cell_w = s + pad
    cell_h = s + pad + label_h
    sheet = Image.new("RGBA",
                      (2 * cell_w + pad,
                       len(people) * cell_h + pad + label_h),
                      (32, 33, 36, 255))
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad // 2), "quiet", fill=(200, 200, 200, 255))
    d.text((pad + cell_w, pad // 2), "speaking", fill=(200, 200, 200, 255))
    for row, (p, sprites) in enumerate(zip(people, sprites_by_person)):
        y0 = label_h + pad + row * cell_h
        for col, level in enumerate((0, LEVELS - 1)):
            img = Image.fromarray(sprites[level], "RGBA")
            sheet.alpha_composite(img, (pad + col * cell_w + pad // 2, y0))
        d.text((pad, y0 + s + 2), p.name, fill=(230, 230, 230, 255))
    sheet.save(out)
    print(f"Contact sheet written: {out}")


# --------------------------------------------------------------------------
# Render one person's overlay
# --------------------------------------------------------------------------

def build_render_cmd(canvas: str, layout: Layout, x: int, y: int, s: int,
                     voice: Path, out_path: Path, n_frames: int) -> list[str]:
    base = ["ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{s}x{s}", "-r", str(layout.fps), "-i", "pipe:0",
            "-i", str(voice)]
    if canvas == "full":
        vf = (f"color=c=0x00000000:s={layout.width}x{layout.height}:"
              f"r={layout.fps}[bg];"
              f"[bg][0:v]overlay=x={x}:y={y}:shortest=1[ov];"
              f"[ov]format=argb[vout]")
        cmd = [*base, "-filter_complex", vf, "-map", "[vout]", "-map", "1:a:0"]
    else:  # tight: encode the sprite stream as-is, position set in Kdenlive
        cmd = [*base, "-map", "0:v", "-map", "1:a:0", "-pix_fmt", "argb"]
    # the voice is already AAC, so copy it instead of re-encoding
    return cmd + ["-c:v", "qtrle", "-c:a", "copy", "-f", "mov",
                  "-frames:v", str(n_frames), str(out_path)]


def _print_summary(person: Person, duration: float, speaking_s: float,
                   aidx: int, note: str, live: bool,
                   gate: Gate | None = None,
                   res: Envelope | None = None) -> None:
    norm_note = ""
    if gate is not None and gate.normalize and res is not None:
        norm_note = (f" [normalised open={res.open_db:.0f}"
                     f"  full={res.full_db:.0f}"
                     f"  close={res.close_db:.0f} dB]")
    line = (f"  {person.name}: {duration:.1f}s, "
            f"~{speaking_s / 60:.1f} min speaking "
            f"(stream {aidx} '{person.stream_title}'){note}{norm_note}")
    print(("\r" + line + " " * 16) if live else line, flush=True)


def _warn_weak_signal(person: Person, duration: float, speaking_s: float,
                      res: Envelope) -> None:
    """Say so, loudly, when the gate can never (or almost never) open."""
    if res.peak_db < res.open_db:
        warn(f"  {person.name}: WARNING: loudness peaks around "
             f"{res.peak_db:.0f} dB but the gate only opens above "
             f"{res.open_db:.0f} dB — the head will never light up. Raise "
             f"the mic gain in OBS for future sessions; for this file use "
             f"--normalize or set open_db/full_db/close_db in this person's "
             f"[[person]] section.")
    elif duration >= 120.0 and speaking_s < max(5.0, 0.002 * duration):
        warn(f"  {person.name}: warning: only {speaking_s:.0f}s of speech "
             f"detected in {_fmt_dur(duration)} — if they talked more than "
             f"that, the gate thresholds are too high for this mic "
             f"(try --normalize).")


def render_overlay(person: Person, idx: int, layout: Layout, gate: Gate,
                   out_path: Path, canvas: str, preview_s: float | None,
                   dry_run: bool, live: bool = True,
                   abort: threading.Event | None = None) -> None:
    aidx = find_audio_index(person.source, person.stream_title)
    full_duration = media_duration(person.source)
    duration = min(full_duration, preview_s) if preview_s else full_duration
    n_frames = int(math.ceil(duration * layout.fps))
    s = layout.sprite
    x, y = layout.cell_origin(idx)
    note = f" [place at X={x} Y={y}, size {s}x{s}]" if canvas == "tight" else ""

    check_track_bleed(person, aidx, full_duration)

    if dry_run:
        res = speaking_envelope(
            person, aidx, layout, gate, n_frames, live,
            limit_s=preview_s, abort=abort)
        speaking_s = float((res.env > 0.15).sum()) / layout.fps
        _print_summary(person, duration, speaking_s, aidx, note, live,
                       gate, res)
        _warn_weak_signal(person, duration, speaking_s, res)
        return

    # fetch the head first so a network or avatar problem fails in seconds,
    # not after a full audio-analysis pass over the source
    head = load_head(person)

    with tempfile.TemporaryDirectory(prefix="indspk_") as td:
        voice = Path(td) / "voice.m4a"
        res = speaking_envelope(
            person, aidx, layout, gate, n_frames, live,
            voice_out=voice, limit_s=preview_s, abort=abort)
        env = res.env
        speaking_s = float((env > 0.15).sum()) / layout.fps
        _print_summary(person, duration, speaking_s, aidx, note, live,
                       gate, res)
        _warn_weak_signal(person, duration, speaking_s, res)

        levels = np.clip((env * (LEVELS - 1)).round().astype(np.int64),
                         0, LEVELS - 1)
        sprites = render_sprites(person, layout, head)
        sprites_f = [arr.astype(np.float32) for arr in sprites]
        tmp_out = out_path.with_name(
            out_path.stem + ".partial" + out_path.suffix)
        cmd = build_render_cmd(canvas, layout, x, y, s, voice, tmp_out, n_frames)
        errlog = Path(td) / "ffmpeg.log"
        rp = Progress(n_frames, f"  {person.name}: rendering", live)

        with open(errlog, "wb") as errf:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=errf)
            if proc.stdin is None:
                die("internal error: ffmpeg stdin not captured")
            try:
                broke = False
                for k, lvl in enumerate(levels):
                    if abort is not None and abort.is_set():
                        raise VoiceError(
                            f"{person.name}: stopped (another overlay failed)")
                    act_f = float(env[k])
                    breath = (1.0 + layout.breath_scale
                              * (1.0 - min(1.0, act_f))
                              * math.sin(2.0 * math.pi
                                         * layout.breath_freq * k / layout.fps))
                    frame = np.clip(
                        sprites_f[int(lvl)] * breath, 0.0, 255.0
                    ).astype(np.uint8)
                    try:
                        proc.stdin.write(frame.tobytes())
                    except BrokenPipeError:
                        broke = True       # ffmpeg died; its log is read below
                        break
                    if (k & 0xFF) == 0:
                        rp.update(k + 1)
                if not broke:
                    rp.update(n_frames, force=True)
                    proc.stdin.close()
                if live:
                    print(f"\r  {person.name}: finalising…" + " " * 24,
                          end="", flush=True)
                ret = proc.wait()
                if ret != 0:
                    tail = errlog.read_text(errors="replace")[-2000:]
                    die(f"{person.name}: ffmpeg render failed:\n{tail}")
                os.replace(tmp_out, out_path)
            except BaseException:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                tmp_out.unlink(missing_ok=True)
                raise

    rp.finish(f"  {person.name}: rendered in {_fmt_dur(rp.elapsed())}"
              f"  ->  {out_path.name}")


# --------------------------------------------------------------------------
# Config loading + source resolution
# --------------------------------------------------------------------------

def load_config(path: Path) -> tuple[Layout, Gate, list[Person], dict]:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    layout = Layout(**{k: v for k, v in cfg.get("project", {}).items()
                       if k in Layout.__dataclass_fields__})
    for k, v in cfg.get("layout", {}).items():
        if k in Layout.__dataclass_fields__:
            setattr(layout, k, v)
    gate = Gate(**{k: v for k, v in cfg.get("gate", {}).items()
                   if k in Gate.__dataclass_fields__})
    base = path.parent
    people: list[Person] = []
    for pc in cfg.get("person", []):
        if "name" not in pc:
            die("each [[person]] needs a name")
        if "source" not in pc and "suffix" not in pc:
            die(f"{pc.get('name')}: needs either source or suffix")
        src = None
        if "source" in pc:
            src = Path(pc["source"])
            if not src.is_absolute():
                src = base / src
        hf = pc.get("head_file")
        if hf and not Path(hf).is_absolute():
            hf = base / hf
        people.append(Person(
            name=pc["name"],
            colour=hex_to_rgb(pc.get("colour", "#33c1ff")),
            source=src,
            suffix=str(pc["suffix"]) if "suffix" in pc else None,
            nick=pc.get("nick"),
            head_file=Path(hf) if hf else None,
            stream_title=pc.get("stream_title",
                                cfg.get("project", {}).get(
                                    "stream_title", "Voice audio")),
            # any [gate] key can be overridden inside a [[person]] section
            gate=replace(gate, **{k: pc[k]
                                  for k in Gate.__dataclass_fields__
                                  if k in pc}),
        ))
    if not people:
        die("config defines no [[person]] sections")
    return layout, gate, people, cfg


def resolve_sources(people: list[Person], indir: Path,
                    date: str | None) -> None:
    """Fill in each person's source file from suffix + (date or glob)."""
    if not indir.is_dir():
        die(f"input directory not found: {indir}")
    for p in people:
        if p.source is not None:
            continue
        if not p.suffix:
            die(f"{p.name}: no source and no suffix to find one")
        if date:
            cand = indir / f"{date}_{p.suffix}.mkv"
            if not cand.is_file():
                die(f"{p.name}: expected {cand} but it does not exist")
            p.source = cand
        else:
            matches = sorted(indir.glob(f"*_{p.suffix}.mkv"))
            if not matches:
                die(f"{p.name}: no file matching *_{p.suffix}.mkv in {indir}")
            if len(matches) > 1:
                names = ", ".join(m.name for m in matches)
                die(f"{p.name}: several files match *_{p.suffix}.mkv in "
                    f"{indir} ({names}). Pass --date YYYY-MM-DD to choose.")
            p.source = matches[0]
    for p in people:
        if p.source is None or not p.source.is_file():
            die(f"{p.name}: could not resolve a source file")


# --------------------------------------------------------------------------
# Stream discovery
# --------------------------------------------------------------------------

def _audio_stream_label(s: dict) -> str:
    title  = s.get("tags", {}).get("title") or "<untitled>"
    codec  = s.get("codec_name", "?")
    ch     = s.get("channels", "?")
    layout = s.get("channel_layout", "")
    br     = s.get("bit_rate")
    info   = [codec, f"{ch}ch"]
    if layout and layout not in ("mono", "stereo"):
        info.append(layout)
    if br:
        info.append(f"{int(br) // 1000}k")
    return f"{title!r:<26}  ({', '.join(info)})"


def discover_stream_titles(people: list[Person]) -> dict[str, str]:
    """Interactively ask the user to identify each person's voice track."""
    choices: dict[str, str] = {}
    for person in people:
        streams = [s for s in ffprobe_streams(person.source)
                   if s.get("codec_type") == "audio"]
        if not streams:
            die(f"{person.name}: {person.source} has no audio streams")
        print(f"\n{person.name}  ({person.source.name})")
        for i, s in enumerate(streams):
            current = (s.get("tags", {}).get("title", "").strip().lower()
                       == person.stream_title.strip().lower())
            marker = "  ← current" if current else ""
            print(f"  {i}: {_audio_stream_label(s)}{marker}")
        while True:
            try:
                raw = input(f"  Voice track [0-{len(streams) - 1}]: ").strip()
                idx = int(raw)
                if 0 <= idx < len(streams):
                    title = streams[idx].get("tags", {}).get("title", "")
                    if not title:
                        print("  Warning: this stream has no title — the match "
                              "may be unreliable. Consider labelling the track "
                              "in OBS before recording.")
                    choices[person.name] = title
                    break
            except (ValueError, EOFError):
                pass
            print(f"  Please enter a number between 0 and {len(streams) - 1}.")
    return choices


def patch_config_stream_titles(config_path: Path, choices: dict[str, str]) -> None:
    """Update or insert per-person stream_title values in the TOML config."""
    text  = config_path.read_text()
    # Split at [[person]] boundaries (lookahead keeps the header in each part)
    parts = re.split(r'(?=^\[\[person\]\])', text, flags=re.M)
    out: list[str] = []
    for part in parts:
        if not part.lstrip().startswith("[[person]]"):
            out.append(part)
            continue
        nm = re.search(r'^name\s*=\s*["\'](.+?)["\']', part, re.M)
        if not nm or nm.group(1) not in choices:
            out.append(part)
            continue
        # json.dumps escaping is valid TOML basic-string escaping; lambda
        # replacements keep re.sub from interpreting backslashes in the title
        entry = f"stream_title = {json.dumps(choices[nm.group(1)])}"
        if re.search(r'^stream_title\s*=', part, re.M):
            part = re.sub(r'^stream_title\s*=.*$',
                          lambda m: entry, part, flags=re.M, count=1)
        else:
            part = re.sub(r'^(name\s*=.+)$',
                          lambda m: f"{m.group(1)}\n{entry}", part,
                          flags=re.M, count=1)
        out.append(part)
    config_path.write_text("".join(out))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="indicate-speaker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path, nargs="?", default=None,
                    help="TOML config file; defaults to indicate-speaker.toml "
                         "next to the script, or the only .toml in the current "
                         "directory if there is exactly one")
    ap.add_argument("--person", action="append", default=[], metavar="NAME",
                    help="only process this person; repeatable")
    ap.add_argument("--canvas", choices=["full", "tight"], default=None,
                    help="tight (default): just the head, ~24x faster and "
                         "smaller, position set once in Kdenlive. full: a "
                         "1920x1080 frame, drop straight on a track but bigger "
                         "and slower.")
    ap.add_argument("--jobs", "-j", type=int, default=1, metavar="N",
                    help="render N people in parallel (4 is sensible on a "
                         "quad-core; default 1)")
    ap.add_argument("--indir", type=Path, default=None, metavar="DIR",
                    help="folder holding the episode's MKVs, found by suffix "
                         "(default: the config file's folder)")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="only needed if one folder holds several episodes")
    ap.add_argument("--outdir", "-o", type=Path, default=None, metavar="DIR",
                    help="where to write overlays (default: alongside sources)")
    ap.add_argument("--contact-sheet", action="store_true",
                    help="write a PNG preview of every head and exit")
    ap.add_argument("--preview", type=float, default=None, metavar="SECONDS",
                    help="only process the first SECONDS, for a quick look")
    ap.add_argument("--discover", action="store_true",
                    help="list each person's audio tracks and interactively "
                         "pick the voice track; optionally saves choices to "
                         "the config, then proceeds with the render")
    ap.add_argument("--normalize", action="store_true", default=False,
                    help="derive open/full thresholds per person from their "
                         "own loudness distribution so quiet mics activate "
                         "the indicator as strongly as loud ones")
    ap.add_argument("--refresh-heads", action="store_true",
                    help="re-download avatar heads instead of using the "
                         "cached copies (use after someone changes their skin)")
    ap.add_argument("--dry-run", action="store_true",
                    help="analyse audio and report, render nothing")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    return ap.parse_args()


def _find_config() -> Path:
    script_default = Path(__file__).resolve().with_name("indicate-speaker.toml")
    if script_default.is_file():
        return script_default
    candidates = sorted(Path.cwd().glob("*.toml"))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        names = ", ".join(c.name for c in candidates)
        die(f"several .toml files found ({names}); pass the one to use as an argument")
    die("no config file found; pass one as an argument or place indicate-speaker.toml "
        "next to the script")


def _main(args: argparse.Namespace) -> None:
    if args.config is None:
        args.config = _find_config()
    if not args.config.is_file():
        die(f"config not found: {args.config}")
    layout, gate, people, cfg = load_config(args.config)
    out_cfg = cfg.get("output", {})
    in_cfg = cfg.get("input", {})

    if args.person:
        wanted = {n.lower() for n in args.person}
        people_sel = [(i, p) for i, p in enumerate(people)
                      if p.name.lower() in wanted]
        if not people_sel:
            die(f"no person matched {args.person}")
    else:
        people_sel = list(enumerate(people))

    if args.normalize:
        gate.normalize = True
        for p in people:
            p.gate.normalize = True

    canvas = args.canvas or out_cfg.get("canvas", "tight")
    indir = (args.indir or (Path(in_cfg["dir"]) if "dir" in in_cfg
                            else args.config.parent))
    date = args.date or in_cfg.get("date")
    outdir = args.outdir or (Path(out_cfg["dir"]) if "dir" in out_cfg
                             else indir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.refresh_heads:
        for _, p in people_sel:
            if p.nick:
                head_cache_path(p.nick).unlink(missing_ok=True)

    if args.contact_sheet:
        contact_sheet([p for _, p in people_sel], layout,
                      outdir / "indicate-speaker_preview.png")
        return

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} not found on PATH. Install FFmpeg (it provides both).")

    resolve_sources([p for _, p in people_sel], indir, date)

    if args.discover:
        if not sys.stdin.isatty():
            die("--discover requires an interactive terminal")
        choices = discover_stream_titles([p for _, p in people_sel])
        print()
        try:
            save = input("Save choices to config? [Y/n]: ").strip().lower()
        except EOFError:
            save = "n"
        if save in ("", "y", "yes"):
            patch_config_stream_titles(args.config, choices)
            print(f"Saved to {args.config.name}")
        for _, person in people_sel:
            if person.name in choices:
                person.stream_title = choices[person.name]
        print()

    print(f"indicate-speaker: {layout.width}x{layout.height} @ "
          f"{layout.fps:g} fps, {len(people_sel)} overlay(s), "
          f"canvas={canvas}, jobs={args.jobs}")

    single = args.jobs <= 1 or len(people_sel) <= 1
    live = single and sys.stdout.isatty()
    abort = threading.Event()

    def job(item: tuple[int, Person]) -> None:
        idx, person = item
        out_path = outdir / f"{person.name.lower()}_speaker.mov"
        try:
            render_overlay(person, idx, layout, person.gate or gate, out_path,
                           canvas, args.preview, args.dry_run, live, abort)
        except BaseException:
            abort.set()      # tell sibling jobs to stop promptly
            raise

    if not single:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = [pool.submit(job, it) for it in people_sel]
            try:
                for fut in as_completed(futs):
                    fut.result()           # re-raise the first failure
            except BaseException:
                for f in futs:
                    f.cancel()             # don't start queued jobs
                raise
    else:
        for it in people_sel:
            job(it)

    if not args.dry_run:
        msg = ("Done. In Kdenlive: import the overlays, align each to its "
               "matching source (group with it, then sync as you already do), "
               "mute the overlay audio, and park them on tracks above the "
               "views. Optionally select them all and create a Sequence so "
               "they become one tidy, still-cuttable object.")
        if canvas == "tight":
            msg += (" Tight mode: on each overlay add a Transform effect, set "
                    "Size to the clip's native pixels and Position to the X/Y "
                    "printed above (X is 0 for all; Y steps down per person). "
                    "Save it as an effect favourite to reapply in one click.")
        print(msg)

    with _warn_lock:
        pending_warnings = list(_warnings)
    if pending_warnings:
        print(f"\n{len(pending_warnings)} audio warning(s) from this run:")
        for w in pending_warnings:
            print(w)


def main() -> None:
    args = parse_args()
    try:
        _main(args)
    except VoiceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted; removed any partial output.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
