"""Who is talking: Silero VAD over each player's voice clips, as edited."""

import av
import numpy as np
from silero_vad_lite import SileroVAD

from .config import die
from .timeline import Entry

RATE = 16000
WINDOW = 512        # samples per Silero call at 16 kHz (32 ms)
ON, OFF = 0.5, 0.35  # hysteresis on speech probability
HANGOVER = 0.20     # s kept "talking" after the probability drops (bridges short pauses)
MIN_BURST = 0.12    # s; shorter bursts (clicks, coughs, keyboard) are dropped


def decode(path: str, stream: int, start: float, dur: float) -> np.ndarray:
    """Mono float32 at RATE for [start, start+dur) seconds of one audio stream."""
    try:
        with av.open(path) as c:
            st = c.streams[stream]
            if st.type != "audio":
                die(f"{path}: stream {stream} is {st.type}, not audio")
            c.seek(int(start / st.time_base), stream=st)   # lands at or before start
            rs = av.AudioResampler(format="flt", layout="mono", rate=RATE)
            chunks, first, got = [], None, 0
            for frame in c.decode(st):
                if frame.pts is None:
                    continue
                if first is None:
                    first = float(frame.pts * frame.time_base)
                for r in rs.resample(frame):
                    chunks.append(r.to_ndarray()[0])
                    got += chunks[-1].size
                if first + got / RATE >= start + dur:
                    break
    except av.FFmpegError as e:
        die(f"cannot decode {path}: {e}")
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    skip = max(0, round((start - (first or start)) * RATE))
    return pcm[skip:skip + round(dur * RATE)]


def smooth(probs: np.ndarray) -> np.ndarray:
    """Speech probability per window -> talking yes/no per window."""
    hang = round(HANGOVER * RATE / WINDOW)
    on = np.zeros(len(probs), bool)
    state, left = False, 0
    for i, p in enumerate(probs):
        if p >= ON:
            state, left = True, hang
        elif state and p < OFF:
            left -= 1
            state = left > 0
        on[i] = state
    edges = np.flatnonzero(np.diff(np.r_[0, on.astype(np.int8), 0]))
    for s, e in zip(edges[::2], edges[1::2]):
        if (e - s - hang) * WINDOW / RATE < MIN_BURST:
            on[s:e] = False
    return on


def speech_windows(pcm: np.ndarray) -> np.ndarray:
    vad = SileroVAD(RATE)
    n = len(pcm) // WINDOW
    probs = np.fromiter((vad.process(memoryview(pcm[i * WINDOW:(i + 1) * WINDOW]))
                         for i in range(n)), np.float32, n)
    return smooth(probs)


# ponytail: no cross-track bleed check; each player has their own mic track.
# Add one (compare simultaneous windows' RMS across players) if a crew member
# starts using speakers instead of headphones.
def talking(entries: list[Entry], fps: float, length: int) -> np.ndarray:
    """Per timeline frame: is this player talking?"""
    out = np.zeros(length, bool)
    for e in entries:
        n = min(e.length, length - e.start)
        if n <= 0:
            continue
        win = speech_windows(decode(e.resource, e.audio_index, e.src_in / fps, n / fps))
        if not len(win):
            continue
        idx = np.minimum((np.arange(n) / fps * RATE / WINDOW).astype(int), len(win) - 1)
        out[e.start:e.start + n] |= win[idx]
    return out
