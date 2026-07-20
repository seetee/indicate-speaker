#!/usr/bin/env -S uv run --script
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
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "Pillow"]
# ///
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

Output is FFV1 in a .mkv by default: FOSS, lossless alpha that Kdenlive
decodes reliably, at roughly half the size of the alternatives. --codec
offers two more, both lossless-with-alpha and decoding byte-identically to
ffv1: utvideo in .mkv (fastest timeline scrubbing) and QuickTime Animation
(qtrle) in .mov (for tools that only take .mov).
(VP9/VP8 alpha WebM is smaller still but its alpha is dropped on decode by
the same FFmpeg backend Kdenlive uses, so it is not offered — it would
import as a black box.)

Two canvas modes:
  --canvas tight  (default) just the head sprite. ~24x faster and ~3x
                  smaller; you set each clip's size + position once in
                  Kdenlive and save it as an effect favourite. The exact
                  X/Y values land, with the import steps, in
                  indicate-speaker_notes.txt next to the overlays.
  --canvas full   a 1920x1080 frame with the head pre-positioned. Drop
                  straight onto a track, no positioning, but much bigger
                  (~3 GB/hour) and slower to encode.

Requirements: Python 3.11+, numpy, Pillow, ffmpeg + ffprobe on PATH.
With uv installed, ./indicate-speaker.py fetches the Python deps itself.

    python3 indicate-speaker.py [CONFIG.toml] [options]

First run and no config yet? --init scans one episode's recordings and
builds the config interactively. Run it on one episode first. Licensed under the GNU AGPL v3 or later (see
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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

__version__ = "1.0"

ANALYSIS_RATE = 8000      # Hz, mono; ample for loudness, cheap to decode
LEVELS = 64               # quantised activation steps -> pre-rendered sprites
FRAME_CACHE_MAX = 512     # memoised breath-dimmed frames (~20 MB at defaults)
HEAD_FETCH_PX = 256       # size to fetch from the skin API before downscaling
MAX_HEAD_BYTES = 4 << 20  # refuse skin-API responses larger than this

_BLOOM_LAYERS = (          # (radius_factor, blur_factor, alpha_factor)
    (0.52, 0.06, 0.90),    # tight bright core
    (0.68, 0.16, 0.55),    # mid halo
    (0.85, 0.32, 0.28),    # wide soft ambient
)

# All three are lossless with alpha and decode byte-identically; they differ
# in size and scrubbing speed. ffv1 is the default: FOSS and half the size;
# qtrle remains for tools that want a .mov.
CODECS = {   # name -> (encoder args, encoder pix_fmt, container ext, muxer)
    "qtrle":   (["-c:v", "qtrle"], "argb", ".mov", "mov"),
    "ffv1":    (["-c:v", "ffv1", "-level", "3", "-slices", "4",
                 "-slicecrc", "1"], "bgra", ".mkv", "matroska"),
    "utvideo": (["-c:v", "utvideo", "-pred", "median"],
                "gbrap", ".mkv", "matroska"),
}


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
    normalize: bool | str = "auto"  # true / false / "auto" (only when needed)
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
    sync_title: str | None = None  # shared track for --sync; None = voice
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
        # FLAC for .flac requests: unlike AAC it has no priming delay, which
        # would otherwise make the matroska muxer shift the video by ~21 ms
        acodec = (["-c:a", "flac"] if voice_out.suffix == ".flac"
                  else ["-c:a", "aac", "-b:a", "128k"])
        cmd += ["-map", f"0:{stream_index}", "-ac", "1", *acodec,
                str(voice_out)]

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
    normalized: bool     # thresholds were derived from the track itself
    db: np.ndarray       # per-frame raw loudness, kept for --plot


def _normalized_thresholds(db: np.ndarray, fps: float, g: Gate,
                           peak_db: float) -> tuple[float, float, float] | None:
    """Derive (open, full, close) from this track's own loudness distribution.

    Speech has a bounded dynamic range: anything more than ~25 dB below this
    track's loud speech (the peak) is not speech. Anchoring the speech/silence
    cutoff to the peak — not the floor — keeps it sane for mics whose noise
    gate turns silence into -180 dB digital zero. Returns None when the track
    has too little contrast (near-empty or constant noise) or under a second
    of detectable audio, so the caller keeps the config thresholds. If the
    head reacts to near-silence, raise norm_low_pct in the config.
    """
    floor_db = float(np.percentile(db, 20))
    if peak_db - floor_db <= 12.0:
        return None
    cut = max(peak_db - 25.0, floor_db + 6.0)
    active = db[db > cut]
    if len(active) < max(64, int(fps)):
        return None
    open_db = float(np.percentile(active, g.norm_low_pct))
    full_db = float(np.percentile(active, g.norm_high_pct))
    return open_db, full_db, min(cut, open_db - 2.0)


def _gated_spring(db: np.ndarray, fps: float, g: Gate, open_db: float,
                  full_db: float, close_db: float) -> np.ndarray:
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
    return out


def _gate_failed(env: np.ndarray, fps: float, open_db: float,
                 peak_db: float) -> bool:
    """True when the fixed thresholds clearly fail this track: the gate can
    never open, or almost no speech registered on a long recording. Mirrors
    the _warn_weak_signal heuristics."""
    if peak_db < open_db:
        return True
    duration = len(env) / fps
    speaking_s = float((env > 0.15).sum()) / fps
    return duration >= 120.0 and speaking_s < max(5.0, 0.002 * duration)


def activation_envelope(db: np.ndarray, fps: float, g: Gate) -> Envelope:
    """Map loudness to a smooth 0..1 speaking activation.

    normalize=true derives all three thresholds — including the close/force-
    silent cutoff — from this person's own loudness distribution rather than
    the config values, so a mic recorded 30 dB quieter than the rest still
    animates. normalize="auto" (the default) uses the config thresholds and
    switches to the derived ones only for tracks where the fixed gate clearly
    fails, so a well-levelled mic keeps its tuned absolute behaviour.
    """
    peak_db = float(np.percentile(db, 99.5))
    thresholds = (g.open_db, g.full_db, g.close_db)
    normalized = False

    if g.normalize is True:
        derived = _normalized_thresholds(db, fps, g, peak_db)
        if derived is not None:
            thresholds, normalized = derived, True
    env = _gated_spring(db, fps, g, *thresholds)

    if g.normalize == "auto" and _gate_failed(env, fps, thresholds[0], peak_db):
        derived = _normalized_thresholds(db, fps, g, peak_db)
        if derived is not None:
            thresholds, normalized = derived, True
            env = _gated_spring(db, fps, g, *thresholds)

    return Envelope(env, *thresholds, peak_db, normalized, db)


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
# Recording sync (--sync)
# --------------------------------------------------------------------------

SYNC_HOP_HZ = 20.0        # envelope rate used for cross-correlation
SYNC_MIN_CORR = 0.30      # minimum normalised correlation to accept
SYNC_MIN_RATIO = 3.0      # peak must beat the best rival lag by this factor


def _sync_envelope(path: Path, stream_index: int,
                   limit_s: float | None) -> np.ndarray:
    """Zero-mean RMS envelope at SYNC_HOP_HZ of one audio stream."""
    hop = int(ANALYSIS_RATE / SYNC_HOP_HZ)
    cmd = ["ffmpeg", "-v", "error"]
    if limit_s:
        cmd += ["-t", f"{limit_s}"]
    cmd += ["-i", str(path), "-map", f"0:{stream_index}", "-ac", "1",
            "-ar", str(ANALYSIS_RATE), "-f", "s16le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode != 0:
        die(f"ffmpeg failed reading sync audio from {path}")
    a = np.frombuffer(out.stdout[:len(out.stdout) & ~1],
                      dtype=np.int16).astype(np.float64)
    n = len(a) // hop
    if n < int(SYNC_HOP_HZ * 60):
        die(f"{path}: under a minute of sync audio; cannot correlate")
    e = np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    return e - e.mean()


def correlate_offset(ref: np.ndarray,
                     other: np.ndarray) -> tuple[float, float, float]:
    """(offset_s, peak, ratio) between two SYNC_HOP_HZ envelopes.

    offset_s is how much later `other`'s recording started than `ref`'s
    (negative = earlier). peak is the normalised correlation at the best
    lag; ratio is peak divided by the best correlation found outside ±2 s
    of it — the confidence that this is a real alignment, not noise.
    """
    n = len(ref) + len(other) - 1
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(ref, nfft)
                     * np.conj(np.fft.rfft(other, nfft)), nfft)
    full = np.concatenate([c[nfft - (len(other) - 1):nfft], c[:len(ref)]])
    lags = np.arange(-(len(other) - 1), len(ref))
    full /= (np.linalg.norm(ref) * np.linalg.norm(other)) or 1e-9
    best = int(np.argmax(full))
    peak = float(full[best])
    guard = int(2 * SYNC_HOP_HZ)
    rival = float(np.delete(full, slice(max(0, best - guard),
                                        best + guard)).max())
    return lags[best] / SYNC_HOP_HZ, peak, peak / max(rival, 1e-9)


def sync_offsets(people: list[Person],
                 limit_s: float | None) -> dict[str, float] | None:
    """Start offsets (seconds, relative to the first person) for every
    person, or None when any pair lacks a trustworthy correlation peak.

    Needs a genuinely shared signal across the recordings — in practice a
    voice-chat (Discord) output recorded as its own track on every machine
    (config: [sync] stream_title). Falls back to the voice track, which
    only works if voice chat is mixed into it.
    """
    envs: dict[str, np.ndarray] = {}
    for p in people:
        title = p.sync_title or p.stream_title
        idx = find_audio_index(p.source, title)
        print(f"  {p.name}: reading sync track '{title}'", flush=True)
        envs[p.name] = _sync_envelope(p.source, idx, limit_s)
    ref = people[0].name
    offsets = {ref: 0.0}
    ok = True
    for p in people[1:]:
        off, peak, ratio = correlate_offset(envs[ref], envs[p.name])
        if peak >= SYNC_MIN_CORR and ratio >= SYNC_MIN_RATIO:
            offsets[p.name] = off
            print(f"  {p.name}: started {off:+.2f}s relative to {ref} "
                  f"(corr {peak:.2f}, {ratio:.0f}x above noise)", flush=True)
        else:
            ok = False
            warn(f"  {p.name}: no reliable shared signal with {ref} "
                 f"(corr {peak:.2f}, ratio {ratio:.1f}x) — align manually; "
                 f"see the README's Automatic sync section for the OBS "
                 f"setup that makes --sync work.")
    return offsets if ok else None


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
        try:
            return Image.open(person.head_file).convert("RGBA")
        except OSError as exc:
            die(f"{person.name}: could not read head_file "
                f"{person.head_file}: {exc}")
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
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:      # Pillow < 10.1: only the tiny fixed-size default
        font = ImageFont.load_default()
    d.text((pad, pad // 2), "quiet", fill=(200, 200, 200, 255), font=font)
    d.text((pad + cell_w, pad // 2), "speaking", fill=(200, 200, 200, 255),
           font=font)
    for row, (p, sprites) in enumerate(zip(people, sprites_by_person)):
        y0 = label_h + pad + row * cell_h
        for col, level in enumerate((0, LEVELS - 1)):
            img = Image.fromarray(sprites[level], "RGBA")
            sheet.alpha_composite(img, (pad + col * cell_w + pad // 2, y0))
        d.text((pad, y0 + s + 2), p.name, fill=(230, 230, 230, 255), font=font)
    sheet.save(out)
    print(f"Contact sheet written: {out}")


def _tick_step(duration_s: float) -> float:
    """Largest step from a pleasant set that yields at most ~14 ticks."""
    for step in (10, 30, 60, 300, 600, 1800, 3600, 7200):
        if duration_s / step <= 14:
            return float(step)
    return 14400.0


def plot_envelope(person: Person, res: Envelope, fps: float,
                  out_png: Path) -> None:
    """Timeline PNG: loudness band, gate thresholds, speaking activation.

    Makes threshold tuning visible: the head lights up exactly where the
    coloured activation area rises, and the dashed lines show where the
    gate sat for this track (after any normalisation).
    """
    W, H = 1600, 260
    ml, mr, mt, mb = 56, 14, 30, 24
    pw, ph = W - ml - mr, H - mt - mb
    DB_MIN, DB_MAX = -80.0, 0.0
    img = Image.new("RGB", (W, H), (24, 25, 28))
    d = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.load_default(size=12)
    except TypeError:      # Pillow < 10.1
        font = ImageFont.load_default()

    def ydb(v: float) -> float:
        return mt + ph * (DB_MAX - min(DB_MAX, max(DB_MIN, v))) / (DB_MAX - DB_MIN)

    n = len(res.db)
    db = np.clip(res.db, DB_MIN, DB_MAX)
    env = np.clip(res.env, 0.0, 1.0)
    cols = np.linspace(0, n, pw + 1).astype(int)
    r, g, b = person.colour

    for x in range(pw):
        lo, hi = cols[x], max(cols[x] + 1, cols[x + 1])
        # activation: translucent filled column in the person's colour
        y_act = mt + ph * (1.0 - float(env[lo:hi].max()))
        d.line([(ml + x, mt + ph), (ml + x, y_act)], fill=(r, g, b, 88))
        # loudness: per-column min..max band
        seg = db[lo:hi]
        d.line([(ml + x, ydb(float(seg.max()))),
                (ml + x, ydb(float(seg.min())))], fill=(126, 128, 134, 200))

    for val, name in ((res.full_db, "full"), (res.open_db, "open"),
                      (res.close_db, "close")):
        y = ydb(val)
        for x0 in range(ml, ml + pw, 10):
            d.line([(x0, y), (x0 + 5, y)], fill=(242, 201, 76, 220))
        d.text((ml + pw - 96, y - 15), f"{name} {val:.0f} dB",
               fill=(242, 201, 76, 255), font=font)

    for v in range(0, int(DB_MIN) - 1, -20):
        d.text((8, ydb(v) - 6), f"{v:4d}", fill=(168, 168, 168), font=font)
    duration = n / fps
    t, step = 0.0, _tick_step(duration)
    while t <= duration:
        x = ml + pw * t / max(duration, 1e-9)
        d.line([(x, mt + ph), (x, mt + ph + 4)], fill=(168, 168, 168))
        d.text((x - 12, mt + ph + 7), _fmt_dur(t),
               fill=(168, 168, 168), font=font)
        t += step

    speaking_min = float((res.env > 0.15).sum()) / fps / 60.0
    # ASCII only: Pillow's default font has no glyph for em-dash and friends
    title = (f"{person.name}: {_fmt_dur(duration)}, "
             f"~{speaking_min:.1f} min speaking"
             + ("   [auto-normalised thresholds]" if res.normalized else ""))
    d.text((ml, 8), title, fill=(232, 232, 232), font=font)
    img.save(out_png)


# --------------------------------------------------------------------------
# Talk statistics
# --------------------------------------------------------------------------

SPEAK_THRESH = 0.15        # activation above this counts as speaking


def speech_bursts(env: np.ndarray, fps: float, merge_gap_s: float = 1.0,
                  min_burst_s: float = 0.3) -> list[tuple[float, float]]:
    """(start_s, end_s) speaking bursts: pauses shorter than merge_gap_s are
    joined into one burst, blips shorter than min_burst_s are dropped."""
    on = env > SPEAK_THRESH
    if not on.any():
        return []
    edges = np.diff(on.astype(np.int8))
    starts = [int(i) + 1 for i in (edges == 1).nonzero()[0]]
    ends = [int(i) + 1 for i in (edges == -1).nonzero()[0]]
    if on[0]:
        starts.insert(0, 0)
    if on[-1]:
        ends.append(len(on))
    merged: list[list[int]] = []
    for s, e in zip(starts, ends):
        if merged and s - merged[-1][1] < merge_gap_s * fps:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s / fps, e / fps) for s, e in merged
            if (e - s) / fps >= min_burst_s]


def write_stats(results: list[tuple[Person, Envelope]], fps: float,
                outdir: Path, episode: Path,
                offsets: dict[str, float] | None) -> None:
    """Write talk_stats.md and talk_stats.csv for the analysed people.

    Cross-person numbers (overlaps, interruptions) need the recordings'
    relative start offsets, which are only known when --sync succeeded;
    without them the report sticks to per-person statistics.
    """
    rows = []
    for person, res in results:
        bursts = speech_bursts(res.env, fps)
        speaking = sum(e - s for s, e in bursts)
        longest = max(bursts, key=lambda b: b[1] - b[0], default=None)
        rows.append({
            "person": person, "bursts": bursts, "speaking": speaking,
            "duration": len(res.env) / fps, "longest": longest,
        })
    total_speech = sum(r["speaking"] for r in rows) or 1e-9

    interruptions: dict[str, int] = {}
    if offsets is not None and len(rows) > 1:
        # shift every burst onto the shared session clock, then count bursts
        # that begin while somebody else is already speaking
        shifted = {r["person"].name: [(s + offsets[r["person"].name],
                                       e + offsets[r["person"].name])
                                      for s, e in r["bursts"]] for r in rows}
        for name, own in shifted.items():
            others = [iv for n, ivs in shifted.items() if n != name
                      for iv in ivs]
            others.sort()
            count = 0
            for s, _ in own:
                count += any(os <= s < oe for os, oe in others)
            interruptions[name] = count

    # a folder literally named "sources" says nothing; use the session name
    ep_name = (episode.parent.name if episode.name.lower() == "sources"
               and episode.parent.name else episode.name)
    md = [f"# Talk stats: {ep_name}", ""]
    header = "| Person | Speaking | Share of episode | Share of speech | Bursts | Longest | Average |"
    if interruptions:
        header = header[:-1] + " Interruptions |"
    md += [header, "|" + "---|" * (header.count("|") - 1)]
    csv = ["person,duration_s,speaking_s,share_of_episode,share_of_speech,"
           "bursts,longest_s,average_s,interruptions"]
    for r in rows:
        name = r["person"].name
        nb = len(r["bursts"])
        avg = r["speaking"] / nb if nb else 0.0
        lg = r["longest"][1] - r["longest"][0] if r["longest"] else 0.0
        line = (f"| {name} | {_fmt_dur(r['speaking'])} "
                f"| {100 * r['speaking'] / max(r['duration'], 1e-9):.0f}% "
                f"| {100 * r['speaking'] / total_speech:.0f}% "
                f"| {nb} | {_fmt_dur(lg)} | {avg:.1f}s |")
        if interruptions:
            line += f" {interruptions.get(name, 0)} |"
        md.append(line)
        csv.append(f"{name},{r['duration']:.1f},{r['speaking']:.1f},"
                   f"{r['speaking'] / max(r['duration'], 1e-9):.3f},"
                   f"{r['speaking'] / total_speech:.3f},{nb},{lg:.1f},"
                   f"{avg:.1f},{interruptions.get(name, '')}")

    md.append("")
    top = max(rows, key=lambda r: r["speaking"], default=None)
    if top and top["speaking"] > 0:
        md.append(f"- Most talkative: **{top['person'].name}** "
                  f"({100 * top['speaking'] / total_speech:.0f}% of all speech)")
    mono = max((r for r in rows if r["longest"]),
               key=lambda r: r["longest"][1] - r["longest"][0], default=None)
    if mono:
        s, e = mono["longest"]
        md.append(f"- Longest monologue: **{mono['person'].name}**, "
                  f"{_fmt_dur(e - s)} (starting at {_fmt_dur(s)} into "
                  f"their recording)")
    if offsets is None and len(rows) > 1:
        md.append("- Cross-person stats (interruptions/overlap) need "
                  "recording offsets: run with --sync on recordings that "
                  "share a voice-chat track.")
    (outdir / "talk_stats.md").write_text("\n".join(md) + "\n")
    (outdir / "talk_stats.csv").write_text("\n".join(csv) + "\n")
    print(f"Talk stats written: {outdir / 'talk_stats.md'} (+ .csv)")


# --------------------------------------------------------------------------
# Render one person's overlay
# --------------------------------------------------------------------------

def build_render_cmd(canvas: str, codec: str, layout: Layout, x: int, y: int,
                     s: int, voice: Path, out_path: Path,
                     n_frames: int) -> list[str]:
    codec_args, pix_fmt, _, muxer = CODECS[codec]
    base = ["ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{s}x{s}", "-r", str(layout.fps), "-i", "pipe:0",
            "-i", str(voice)]
    if canvas == "full":
        vf = (f"color=c=0x00000000:s={layout.width}x{layout.height}:"
              f"r={layout.fps}[bg];"
              f"[bg][0:v]overlay=x={x}:y={y}:shortest=1[ov];"
              f"[ov]format={pix_fmt}[vout]")
        cmd = [*base, "-filter_complex", vf, "-map", "[vout]", "-map", "1:a:0"]
    else:  # tight: encode the sprite stream as-is, position set in Kdenlive
        cmd = [*base, "-map", "0:v", "-map", "1:a:0", "-pix_fmt", pix_fmt]
    # the voice is already AAC, so copy it instead of re-encoding
    return cmd + [*codec_args, "-c:a", "copy", "-f", muxer,
                  "-frames:v", str(n_frames), str(out_path)]


def _print_summary(person: Person, duration: float, speaking_s: float,
                   aidx: int, note: str, live: bool,
                   gate: Gate | None = None,
                   res: Envelope | None = None) -> None:
    norm_note = ""
    if res is not None and res.normalized:
        auto = "auto-" if gate is not None and gate.normalize == "auto" else ""
        norm_note = (f" [{auto}normalised open={res.open_db:.0f}"
                     f"  full={res.full_db:.0f}"
                     f"  close={res.close_db:.0f} dB]")
    line = (f"  {person.name}: {duration:.1f}s, "
            f"~{speaking_s / 60:.1f} min speaking "
            f"(stream {aidx} '{person.stream_title}'){note}{norm_note}")
    print(("\r" + line + " " * 16) if live else line, flush=True)


def _warn_weak_signal(person: Person, duration: float, speaking_s: float,
                      res: Envelope) -> bool:
    """Say so, loudly, when the gate can never (or almost never) open.
    Returns True when a warning was issued (callers then auto-write the
    envelope plot so the problem is visible without a --plot re-run)."""
    if res.peak_db < res.open_db:
        hint = ("even the track's own statistics found no usable speech — "
                "check the recording"
                if res.normalized else
                "for this file use --normalize or set open_db/full_db/"
                "close_db in this person's [[person]] section")
        warn(f"  {person.name}: WARNING: loudness peaks around "
             f"{res.peak_db:.0f} dB but the gate only opens above "
             f"{res.open_db:.0f} dB — the head will never light up. Raise "
             f"the mic gain in OBS for future sessions; {hint}.")
        return True
    elif duration >= 120.0 and speaking_s < max(5.0, 0.002 * duration):
        hint = "" if res.normalized else " (try --normalize)"
        warn(f"  {person.name}: warning: only {speaking_s:.0f}s of speech "
             f"detected in {_fmt_dur(duration)} — if they talked more than "
             f"that, the gate thresholds are too high for this mic{hint}.")
        return True
    return False


def write_overlay_notes(outdir: Path, people_sel: list[tuple[int, Person]],
                        layout: Layout, canvas: str, codec: str) -> Path:
    """Write the Kdenlive import steps (and, in tight mode, each overlay's
    Transform position) to a text file next to the overlays, so the values
    survive the console scrolling away."""
    ext = CODECS[codec][2]
    s = layout.sprite
    lines = ["indicate-speaker overlay notes",
             "==============================",
             "",
             f"Overlays (canvas={canvas}, codec={codec}):",
             ""]
    width = max(len(p.name) for _, p in people_sel) + len(f"_speaker{ext}")
    for idx, person in people_sel:
        row = f"  {f'{person.name.lower()}_speaker{ext}':<{width}}"
        if canvas == "tight":
            x, y = layout.cell_origin(idx)
            row += f"   X={x}  Y={y}  size {s}x{s}"
        lines.append(row.rstrip())
    lines += ["",
              "In Kdenlive:",
              "  1. Import the overlays and align each to its matching source",
              "     clip (group with it, then sync as you already do).",
              "  2. Mute the overlay audio and park them on tracks above the",
              "     views."]
    if canvas == "tight":
        lines += [
              "  3. On each overlay add a Transform effect, set Size to the",
              "     clip's native pixels and Position to the X/Y above (X is",
              "     the same for all; Y steps down per person). Save it as an",
              "     effect favourite to reapply in one click.",
              "  4. Optionally select them all and create a Sequence so they",
              "     become one tidy, still-cuttable object that never moves."]
    else:
        lines += [
              "  3. Full canvas: drop straight on a track, no positioning.",
              "  4. Optionally select them all and create a Sequence so they",
              "     become one tidy, still-cuttable object that never moves."]
    notes = outdir / "indicate-speaker_notes.txt"
    notes.write_text("\n".join(lines) + "\n")
    return notes


def render_overlay(person: Person, idx: int, layout: Layout, gate: Gate,
                   out_path: Path, canvas: str, codec: str,
                   preview_s: float | None, dry_run: bool, live: bool = True,
                   plot: bool = False,
                   abort: threading.Event | None = None) -> Envelope:
    aidx = find_audio_index(person.source, person.stream_title)
    full_duration = media_duration(person.source)
    duration = min(full_duration, preview_s) if preview_s else full_duration
    n_frames = int(math.ceil(duration * layout.fps))
    s = layout.sprite
    x, y = layout.cell_origin(idx)
    note = f" [place at X={x} Y={y}, size {s}x{s}]" if canvas == "tight" else ""

    # the bleed check decodes minutes from the middle of the full recording —
    # too slow for a quick look, so previews skip it
    if preview_s is None:
        check_track_bleed(person, aidx, full_duration)

    def maybe_plot(res: Envelope, force: bool = False) -> None:
        if plot or force:
            png = out_path.with_name(f"{person.name.lower()}_envelope.png")
            plot_envelope(person, res, layout.fps, png)
            why = "" if plot else " (to help diagnose the warning)"
            print(f"  {person.name}: envelope plot{why}  ->  {png.name}",
                  flush=True)

    if dry_run:
        res = speaking_envelope(
            person, aidx, layout, gate, n_frames, live,
            limit_s=preview_s, abort=abort)
        speaking_s = float((res.env > 0.15).sum()) / layout.fps
        _print_summary(person, duration, speaking_s, aidx, note, live,
                       gate, res)
        warned = _warn_weak_signal(person, duration, speaking_s, res)
        maybe_plot(res, force=warned)
        return res

    # fetch the head first so a network or avatar problem fails in seconds,
    # not after a full audio-analysis pass over the source
    head = load_head(person)

    with tempfile.TemporaryDirectory(prefix="indspk_") as td:
        voice = Path(td) / ("voice.m4a" if CODECS[codec][3] == "mov"
                            else "voice.flac")
        res = speaking_envelope(
            person, aidx, layout, gate, n_frames, live,
            voice_out=voice, limit_s=preview_s, abort=abort)
        env = res.env
        speaking_s = float((env > 0.15).sum()) / layout.fps
        _print_summary(person, duration, speaking_s, aidx, note, live,
                       gate, res)
        warned = _warn_weak_signal(person, duration, speaking_s, res)
        maybe_plot(res, force=warned)

        levels = np.clip((env * (LEVELS - 1)).round().astype(np.int64),
                         0, LEVELS - 1)
        sprites = render_sprites(person, layout, head)
        sprites_f = [arr.astype(np.float32) for arr in sprites]
        sprite_bytes = [arr.tobytes() for arr in sprites]
        frame_cache: dict[tuple[int, int], bytes] = {}
        tmp_out = out_path.with_name(
            out_path.stem + ".partial" + out_path.suffix)
        cmd = build_render_cmd(canvas, codec, layout, x, y, s, voice, tmp_out,
                               n_frames)
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
                    li = int(lvl)
                    if breath == 1.0:      # fully speaking, or breathing off
                        payload = sprite_bytes[li]
                    else:
                        # breath quantised to 1/256 — below one 8-bit output
                        # step — so the handful of recurring dim states become
                        # cache hits instead of per-frame array maths
                        key = (li, round(breath * 256))
                        payload = frame_cache.get(key)
                        if payload is None:
                            payload = np.clip(
                                sprites_f[li] * (key[1] / 256.0), 0.0, 255.0
                            ).astype(np.uint8).tobytes()
                            if len(frame_cache) < FRAME_CACHE_MAX:
                                frame_cache[key] = payload
                    try:
                        proc.stdin.write(payload)
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
    return res


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
    if gate.normalize not in (True, False, "auto"):
        die(f'[gate] normalize must be true, false or "auto" '
            f'(got {gate.normalize!r})')
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
        # any [gate] key can be overridden inside a [[person]] section
        pgate = replace(gate, **{k: pc[k] for k in Gate.__dataclass_fields__
                                 if k in pc})
        if pgate.normalize not in (True, False, "auto"):
            die(f'{pc["name"]}: normalize must be true, false or "auto" '
                f'(got {pgate.normalize!r})')
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
            sync_title=pc.get("sync_title",
                              cfg.get("sync", {}).get("stream_title")),
            gate=pgate,
        ))
    if not people:
        die("config defines no [[person]] sections")
    return layout, gate, people, cfg


# the naming contract for source recordings: YYYY-MM-DD_<suffix>.mkv
_EPISODE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(.+)\.mkv$")


def episode_suffixes(indir: Path) -> dict[str, Path]:
    """The newest episode's recordings directly in `indir`, keyed by
    filename suffix (the k in YYYY-MM-DD_k.mkv)."""
    by_date: dict[str, dict[str, Path]] = {}
    for f in sorted(indir.glob("*.mkv")):
        m = _EPISODE_RE.match(f.name)
        if m:
            by_date.setdefault(m.group(1), {})[m.group(2)] = f
    return by_date[max(by_date)] if by_date else {}


def find_episode_dir(indir: Path, people: list[Person],
                     date: str | None) -> Path:
    """Resolve the episode folder: `indir` itself when it already holds the
    MKVs, otherwise the newest complete episode found up to two levels below
    it (so the config can point at an episodes root like
    .../sessions/session_NN_DATE/sources/ and a plain run just works)."""
    if not indir.is_dir():
        die(f"input directory not found: {indir}")
    suffixes = {p.suffix for p in people if p.source is None and p.suffix}
    if not suffixes:
        return indir
    if any(any(indir.glob(f"*_{s}.mkv")) for s in suffixes):
        return indir     # an episode folder was given directly

    episodes: dict[tuple[str, Path], set[str]] = {}
    for pattern in ("*/*.mkv", "*/*/*.mkv"):
        for f in indir.glob(pattern):
            m = _EPISODE_RE.match(f.name)
            if m and m.group(2) in suffixes:
                episodes.setdefault((m.group(1), f.parent),
                                    set()).add(m.group(2))
    complete = {k for k, v in episodes.items() if v == suffixes}
    if not complete:
        pat = ", ".join(f"*_{s}.mkv" for s in sorted(suffixes))
        die(f"no complete episode found in {indir} or up to two levels "
            f"below it (need {pat} together in one folder). Pass --indir "
            f"with the episode's folder.")
    if date:
        dirs = sorted(d for dt, d in complete if dt == date)
        if not dirs:
            dates = ", ".join(sorted({dt for dt, _ in complete}))
            die(f"no episode dated {date} under {indir}; found: {dates}")
    else:
        date = max(dt for dt, _ in complete)
        dirs = sorted(d for dt, d in complete if dt == date)
    if len(dirs) > 1:
        names = ", ".join(str(d) for d in dirs)
        die(f"several folders hold an episode dated {date}: {names}. "
            f"Pass --indir to choose one.")
    print(f"Using episode: {dirs[0]}")
    return dirs[0]


def ask_episode_dir(indir: Path, people: list[Person],
                    date: str | None) -> Path:
    """find_episode_dir, but on failure ask for the sources folder instead of
    giving up — the script is normally started inside an episode's sources/
    directory, and a run from anywhere else should just prompt."""
    while True:
        try:
            return find_episode_dir(indir, people, date)
        except VoiceError as exc:
            if not sys.stdin.isatty():
                raise
            print(exc)
            try:
                raw = input("Episode sources directory (blank to abort): ").strip()
            except EOFError:
                raw = ""
            if not raw:
                raise
            indir = Path(raw).expanduser()


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
# First-run setup (--init)
# --------------------------------------------------------------------------

_INIT_COLOURS = ("#33c1ff", "#ff9f43", "#2ecc71", "#e84393",
                 "#f1c40f", "#9b59b6", "#e74c3c", "#1abc9c")


def init_config_text(entries: list[dict[str, str]]) -> str:
    """A starter TOML for --init: commented defaults plus one [[person]]
    per entry (keys: name, suffix, nick, stream_title). Written textually,
    like the rest of the config handling, so comments survive later
    --discover patches."""
    parts = [
        "# indicate-speaker configuration (generated by --init)\n"
        "# Uncommented values below are the defaults; see the README for\n"
        "# every option.\n"
        "\n"
        "# [layout]\n"
        "# head_size = 56      # pixel size of a speaking head\n"
        "# gap = 8             # vertical gap between heads\n"
        "\n"
        "# [gate]\n"
        '# normalize = "auto"  # per-person thresholds when the fixed gate fails\n'
        "# open_db = -38.0     # envelope starts opening above this\n"
        "# full_db = -16.0     # fully lit at/above this\n"
        "# close_db = -46.0    # forced toward silence below this\n"
        "\n"
        "# [output]\n"
        '# codec = "ffv1"      # or "utvideo" (.mkv, fast) / "qtrle" (.mov)\n'
        "# jobs = 1            # render this many people in parallel\n"]
    for i, e in enumerate(entries):
        parts.append(
            "\n[[person]]\n"
            f"name = {json.dumps(e['name'])}\n"
            f"suffix = {json.dumps(e['suffix'])}"
            f"  # matches YYYY-MM-DD_{e['suffix']}.mkv\n"
            f"nick = {json.dumps(e['nick'])}"
            f"  # Minecraft name, used to fetch the head\n"
            f"stream_title = {json.dumps(e['stream_title'])}\n"
            f'colour = "{_INIT_COLOURS[i % len(_INIT_COLOURS)]}"\n')
    return "".join(parts)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def init_wizard(target: Path, indir: Path) -> None:
    """Interactively build a starter config from one episode's recordings."""
    if target.exists():
        die(f"{target} already exists; --init refuses to overwrite it. "
            f"Use --discover to update voice tracks instead.")
    files = episode_suffixes(indir) if indir.is_dir() else {}
    while not files:
        print(f"No YYYY-MM-DD_<suffix>.mkv recordings found in {indir}.")
        raw = _ask("Episode sources directory (blank to abort): ")
        if not raw:
            die("aborted: --init needs a folder with one episode's MKV files")
        indir = Path(raw).expanduser()
        files = episode_suffixes(indir) if indir.is_dir() else {}

    print(f"Found {len(files)} recording(s) in {indir}.")
    people: list[Person] = []
    for suffix, f in sorted(files.items()):
        name = _ask(f"  {f.name}: person's name (blank to skip): ")
        if not name:
            continue
        if any(p.name.lower() == name.lower() for p in people):
            die(f"two recordings can't both belong to {name}")
        nick = _ask(f"    Minecraft nick [{name}]: ") or name
        people.append(Person(name=name, colour=(0, 0, 0), source=f,
                             suffix=suffix, nick=nick))
    if not people:
        die("aborted: no people entered")

    choices = discover_stream_titles(people)
    target.write_text(init_config_text(
        [{"name": p.name, "suffix": p.suffix or "", "nick": p.nick or "",
          "stream_title": choices.get(p.name, "Voice audio")}
         for p in people]))
    print(f"\nWrote {target}")
    print("Next: check the heads with --contact-sheet, then render a quick "
          "sample with --preview 30.")


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
                         "directory if there is exactly one. A directory here "
                         "is treated as --indir instead.")
    ap.add_argument("--person", action="append", default=[], metavar="NAME",
                    help="only process this person; repeatable")
    ap.add_argument("--codec", choices=sorted(CODECS), default=None,
                    help="overlay video codec: ffv1 (default, .mkv, smallest, "
                         "archival-grade), utvideo (.mkv, fastest timeline "
                         "scrubbing), qtrle (.mov, for tools that only take "
                         ".mov). All lossless with alpha.")
    ap.add_argument("--canvas", choices=["full", "tight"], default=None,
                    help="tight (default): just the head, ~24x faster and "
                         "smaller, position set once in Kdenlive. full: a "
                         "1920x1080 frame, drop straight on a track but bigger "
                         "and slower.")
    ap.add_argument("--jobs", "-j", type=int, default=None, metavar="N",
                    help="render N people in parallel (default: the config's "
                         "[output] jobs, else 1; use 1 on slow spinning disks)")
    ap.add_argument("--indir", type=Path, default=None, metavar="DIR",
                    help="folder holding the episode's MKVs, found by suffix "
                         "(default: the current directory; prompts if no "
                         "episode is found there)")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="only needed if one folder holds several episodes")
    ap.add_argument("--outdir", "-o", type=Path, default=None, metavar="DIR",
                    help="where to write overlays (default: alongside sources)")
    ap.add_argument("--contact-sheet", action="store_true",
                    help="write a PNG preview of every head and exit")
    ap.add_argument("--preview", type=float, default=None, metavar="SECONDS",
                    help="only process the first SECONDS, for a quick look")
    ap.add_argument("--init", action="store_true",
                    help="first-run setup: scan --indir (default: the "
                         "current directory) for YYYY-MM-DD_<suffix>.mkv "
                         "recordings, ask who each belongs to, pick voice "
                         "tracks, write a starter config, and exit")
    ap.add_argument("--discover", action="store_true",
                    help="list each person's audio tracks and interactively "
                         "pick the voice track; optionally saves choices to "
                         "the config, then proceeds with the render")
    ap.add_argument("--normalize", action="store_true", default=False,
                    help="force per-person thresholds for everyone (by "
                         "default, normalize=\"auto\" applies them only to "
                         "tracks where the configured gate clearly fails)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave finished overlay files alone instead of "
                         "re-rendering them (cheap retry after one person's "
                         "render failed)")
    ap.add_argument("--refresh-heads", action="store_true",
                    help="re-download avatar heads instead of using the "
                         "cached copies (use after someone changes their skin)")
    ap.add_argument("--sync", action="store_true",
                    help="compute each recording's start offset by "
                         "cross-correlating a shared audio track (see the "
                         "README's Automatic sync section); refuses rather "
                         "than guesses when no reliable shared signal exists")
    ap.add_argument("--sync-window", type=float, default=1800.0,
                    metavar="SECONDS",
                    help="how much of each recording --sync reads "
                         "(default 1800; 0 = the whole file)")
    ap.add_argument("--stats", action="store_true",
                    help="write talk_stats.md/.csv (per-person talk time, "
                         "share, longest monologue) next to the overlays; "
                         "combine with --dry-run to skip rendering")
    ap.add_argument("--plot", action="store_true",
                    help="write a per-person envelope timeline PNG (loudness, "
                         "thresholds, speaking activation) next to the "
                         "overlays — makes gate tuning visible")
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
    if args.config is not None and args.config.is_dir():
        # a directory as the positional argument means --indir
        if args.indir is None:
            args.indir = args.config
        args.config = None
    if args.init:
        if not sys.stdin.isatty():
            die("--init requires an interactive terminal")
        target = args.config or Path(__file__).resolve().with_name(
            "indicate-speaker.toml")
        init_wizard(target, args.indir or Path.cwd())
        return
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
    codec = args.codec or out_cfg.get("codec", "ffv1")
    if codec not in CODECS:
        die(f"[output] codec must be one of {', '.join(sorted(CODECS))} "
            f"(got {codec!r})")
    indir = (args.indir or (Path(in_cfg["dir"]) if "dir" in in_cfg
                            else Path.cwd()))
    date = args.date or in_cfg.get("date")
    jobs = args.jobs if args.jobs is not None else int(out_cfg.get("jobs", 1))

    if args.refresh_heads:
        for _, p in people_sel:
            if p.nick:
                head_cache_path(p.nick).unlink(missing_ok=True)

    if args.contact_sheet:
        outdir = (args.outdir or (Path(out_cfg["dir"]) if "dir" in out_cfg
                                  else args.config.parent))
        outdir.mkdir(parents=True, exist_ok=True)
        contact_sheet([p for _, p in people_sel], layout,
                      outdir / "indicate-speaker_preview.png")
        return

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} not found on PATH. Install FFmpeg (it provides both).")

    indir = ask_episode_dir(indir, [p for _, p in people_sel], date)
    resolve_sources([p for _, p in people_sel], indir, date)

    outdir = args.outdir or (Path(out_cfg["dir"]) if "dir" in out_cfg
                             else indir)
    outdir.mkdir(parents=True, exist_ok=True)

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

    offsets: dict[str, float] | None = None
    if args.sync:
        window = args.sync_window or None
        print("Sync: cross-correlating recordings"
              + (f" (first {_fmt_dur(window)} of each)" if window else ""))
        offsets = sync_offsets([p for _, p in people_sel], window)

    print(f"indicate-speaker: {layout.width}x{layout.height} @ "
          f"{layout.fps:g} fps, {len(people_sel)} overlay(s), "
          f"canvas={canvas}, codec={codec}, jobs={jobs}")

    single = jobs <= 1 or len(people_sel) <= 1
    live = single and sys.stdout.isatty()
    abort = threading.Event()
    results_lock = threading.Lock()
    results: list[tuple[int, Person, Envelope]] = []

    def job(item: tuple[int, Person]) -> None:
        idx, person = item
        out_path = outdir / f"{person.name.lower()}_speaker{CODECS[codec][2]}"
        if args.skip_existing and out_path.is_file() and not args.dry_run:
            print(f"  {person.name}: exists, skipped ({out_path.name})",
                  flush=True)
            return
        try:
            res = render_overlay(person, idx, layout, person.gate or gate,
                                 out_path, canvas, codec, args.preview,
                                 args.dry_run, live, args.plot, abort)
        except BaseException:
            abort.set()      # tell sibling jobs to stop promptly
            raise
        with results_lock:
            results.append((idx, person, res))

    if not single:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=jobs) as pool:
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

    if args.stats and results:
        results.sort(key=lambda t: t[0])     # config (screen) order
        write_stats([(p, r) for _, p, r in results], layout.fps,
                    outdir, indir, offsets)

    if not args.dry_run:
        notes = write_overlay_notes(outdir, people_sel, layout, canvas, codec)
        print(f"Done. Kdenlive import steps"
              + (" + positions" if canvas == "tight" else "")
              + f"  ->  {notes}")

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
