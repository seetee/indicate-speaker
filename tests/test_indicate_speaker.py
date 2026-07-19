"""Tests for indicate-speaker.py.

Run with pytest:   uv run --with pytest --with numpy --with pillow -m pytest tests/ -q
Or without pytest: python3 tests/test_indicate_speaker.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import numpy as np

try:
    import pytest
except ImportError:            # plain-python fallback runner at the bottom
    pytest = None

_SCRIPT = Path(__file__).resolve().parents[1] / "indicate-speaker.py"
_spec = importlib.util.spec_from_file_location("indspk", _SCRIPT)
M = importlib.util.module_from_spec(_spec)
sys.modules["indspk"] = M      # required: dataclass processing looks it up
_spec.loader.exec_module(M)


def _skip(msg: str) -> None:
    if pytest is not None:
        pytest.skip(msg)
    print(f"  SKIP: {msg}")


def _expect_die(fn, *needles: str) -> None:
    try:
        fn()
    except M.VoiceError as e:
        for n in needles:
            assert n in str(e), f"'{n}' not in error: {e}"
        return
    raise AssertionError("expected VoiceError")


def _people(*suffixes: str) -> list:
    return [M.Person(name=s.upper(), colour=(255, 0, 0), suffix=s)
            for s in suffixes]


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------

def test_hex_to_rgb() -> None:
    assert M.hex_to_rgb("#ff4da6") == (255, 77, 166)
    assert M.hex_to_rgb("abc") == (170, 187, 204)
    _expect_die(lambda: M.hex_to_rgb("#12345"), "bad colour")


def test_head_cache_path_is_filesystem_safe() -> None:
    p = M.head_cache_path("weird/../nick?x")
    # no separators may survive quoting → the path cannot escape the cache dir
    assert "/" not in p.name and p.parent.name == "indicate-speaker"


# --------------------------------------------------------------------------
# config patching and validation
# --------------------------------------------------------------------------

def test_patch_config_stream_titles_escapes_hostile_titles() -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "cfg.toml"
        cfg.write_text(
            '# a comment to preserve\n'
            '[project]\nstream_title = "Track2"\n\n'
            '[[person]]\nname = "Alice"\nstream_title = "old"\nsuffix = "a"\n\n'
            '[[person]]\nname = "Bob"\nsuffix = "b"\n')
        nasty1 = 'He said "hi" \\1 back\\slash'
        nasty2 = 'Mic \\g<0> "quoted"'
        M.patch_config_stream_titles(cfg, {"Alice": nasty1, "Bob": nasty2})
        parsed = tomllib.loads(cfg.read_text())
        assert parsed["person"][0]["stream_title"] == nasty1
        assert parsed["person"][1]["stream_title"] == nasty2
        assert "# a comment to preserve" in cfg.read_text()


def test_load_config_validates_normalize() -> None:
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.toml"
        bad.write_text('[gate]\nnormalize = "sometimes"\n'
                       '[[person]]\nname = "A"\nsuffix = "a"\nnick = "x"\n')
        _expect_die(lambda: M.load_config(bad),
                    "normalize must be true, false or")
        good = Path(td) / "good.toml"
        good.write_text('[[person]]\nname = "A"\nsuffix = "a"\nnick = "x"\n'
                        'normalize = false\n')
        _, gate, people, _ = M.load_config(good)
        assert gate.normalize == "auto"          # the default
        assert people[0].gate.normalize is False  # per-person override


# --------------------------------------------------------------------------
# episode discovery
# --------------------------------------------------------------------------

def _episode_tree(root: Path) -> None:
    for ses, date in (("session_25", "2025-05-23"),
                      ("session_26", "2025-06-30")):
        d = root / ses / "sources"
        d.mkdir(parents=True)
        for s in "khrj":
            (d / f"{date}_{s}.mkv").touch()


def test_find_episode_dir_picks_newest_and_respects_date() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _episode_tree(root)
        assert (M.find_episode_dir(root, _people("k", "h", "r", "j"), None)
                == root / "session_26" / "sources")
        assert (M.find_episode_dir(root, _people("k", "h", "r", "j"),
                                   "2025-05-23")
                == root / "session_25" / "sources")
        direct = root / "session_25" / "sources"
        assert M.find_episode_dir(direct, _people("k", "h", "r", "j"),
                                  None) == direct


def test_find_episode_dir_skips_incomplete_episodes() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _episode_tree(root)
        (root / "session_26" / "sources" / "2025-06-30_j.mkv").unlink()
        # newest lacks one person → the older complete episode wins
        assert (M.find_episode_dir(root, _people("k", "h", "r", "j"), None)
                == root / "session_25" / "sources")
        # but selecting only the people it does have still finds it
        assert (M.find_episode_dir(root, _people("k", "h"), None)
                == root / "session_26" / "sources")


def test_find_episode_dir_errors() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _episode_tree(root)
        _expect_die(lambda: M.find_episode_dir(root, _people("x"), None),
                    "no complete episode", "*_x.mkv")
        _expect_die(lambda: M.find_episode_dir(
            root, _people("k", "h", "r", "j"), "2024-01-01"),
            "no episode dated 2024-01-01", "2025-05-23")
        dup = root / "copy" / "sources"
        dup.mkdir(parents=True)
        for s in "khrj":
            (dup / f"2025-05-23_{s}.mkv").touch()
        _expect_die(lambda: M.find_episode_dir(
            root, _people("k", "h", "r", "j"), "2025-05-23"),
            "several folders", "2025-05-23")


# --------------------------------------------------------------------------
# activation envelope / adaptive normalize
# --------------------------------------------------------------------------

def _tracks() -> tuple[np.ndarray, np.ndarray, float]:
    rng = np.random.default_rng(42)
    fps, dur = 60.0, 300
    n = int(fps * dur)
    speech = rng.random(n) < 0.3

    def track(speech_db: float, floor_db: float) -> np.ndarray:
        db = np.full(n, floor_db) + rng.normal(0, 1.5, n)
        db[speech] = speech_db + rng.normal(0, 3.0, n)[speech]
        return db

    healthy = track(-20.0, -70.0)   # well inside the -38/-16 gate
    quiet = track(-45.0, -180.0)    # 25 dB too quiet, noise-gated silence
    return healthy, quiet, fps


def test_auto_normalize_leaves_healthy_tracks_alone() -> None:
    healthy, quiet, fps = _tracks()
    g = M.Gate()
    assert g.normalize == "auto"
    r = M.activation_envelope(healthy, fps, g)
    assert not r.normalized and r.open_db == g.open_db
    rq = M.activation_envelope(quiet, fps, g)
    assert rq.normalized, "quiet track must auto-normalize"
    # both synthetic tracks talk ~30% of the time; activation should agree
    speak = float((r.env > 0.15).sum()) / fps
    speak_q = float((rq.env > 0.15).sum()) / fps
    assert 0.5 < speak_q / speak < 1.5, (speak, speak_q)


def test_normalize_true_and_false_are_unconditional() -> None:
    healthy, quiet, fps = _tracks()
    assert M.activation_envelope(healthy, fps,
                                 M.Gate(normalize=True)).normalized
    r = M.activation_envelope(quiet, fps, M.Gate(normalize=False))
    assert not r.normalized and r.open_db == -38.0


# --------------------------------------------------------------------------
# recording sync
# --------------------------------------------------------------------------

def test_correlate_offset_recovers_known_shift() -> None:
    rng = np.random.default_rng(3)
    hop = M.SYNC_HOP_HZ
    session = rng.normal(0, 1, int(hop * 1800))        # 30 min shared signal
    ref = session[: int(hop * 1500)]
    k0 = int(hop * 137.4)                              # other starts 137.4 s later
    other = session[k0:k0 + int(hop * 1500)] + rng.normal(0, 0.5, int(hop * 1500))
    off, peak, ratio = M.correlate_offset(ref, other)
    assert abs(off - 137.4) < 1.0 / hop * 2, off
    assert peak >= M.SYNC_MIN_CORR and ratio >= M.SYNC_MIN_RATIO, (peak, ratio)


def test_correlate_offset_rejects_unrelated_signals() -> None:
    rng = np.random.default_rng(4)
    a = rng.normal(0, 1, int(M.SYNC_HOP_HZ * 900))
    b = rng.normal(0, 1, int(M.SYNC_HOP_HZ * 900))
    _, peak, ratio = M.correlate_offset(a, b)
    assert peak < M.SYNC_MIN_CORR or ratio < M.SYNC_MIN_RATIO, (peak, ratio)


# --------------------------------------------------------------------------
# talk stats
# --------------------------------------------------------------------------

def test_speech_bursts_merges_and_filters() -> None:
    fps = 10.0
    env = np.zeros(300)             # 30 s at 10 fps
    env[10:50] = 1.0                # 1.0s..5.0s   burst A
    env[55:90] = 1.0                # 5.5s..9.0s   gap 0.5s -> merged into A
    env[150:152] = 1.0              # 0.2s blip -> dropped
    env[200:260] = 1.0              # 20.0s..26.0s burst B
    bursts = M.speech_bursts(env, fps)
    assert len(bursts) == 2, bursts
    (a0, a1), (b0, b1) = bursts
    assert abs(a0 - 1.0) < 0.2 and abs(a1 - 9.0) < 0.2, bursts
    assert abs(b0 - 20.0) < 0.2 and abs(b1 - 26.0) < 0.2, bursts
    assert M.speech_bursts(np.zeros(100), fps) == []


def test_write_stats_report() -> None:
    fps = 10.0
    env_a = np.zeros(600)
    env_a[0:300] = 1.0              # 30 s of speech
    env_b = np.zeros(600)
    env_b[400:500] = 1.0            # 10 s of speech
    db = np.full(600, -30.0)
    res_a = M.Envelope(env_a, -38.0, -16.0, -46.0, -20.0, False, db)
    res_b = M.Envelope(env_b, -38.0, -16.0, -46.0, -20.0, False, db)
    pa = M.Person(name="Alice", colour=(255, 0, 0), suffix="a")
    pb = M.Person(name="Bob", colour=(0, 255, 0), suffix="b")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        M.write_stats([(pa, res_a), (pb, res_b)], fps, out,
                      out / "session_9" / "sources", offsets=None)
        md = (out / "talk_stats.md").read_text()
        csv = (out / "talk_stats.csv").read_text()
    assert "# Talk stats: session_9" in md
    assert "Most talkative: **Alice** (75% of all speech)" in md, md
    assert "need recording offsets" in md
    assert "Alice,60.0,30.0,0.500,0.750,1,30.0,30.0," in csv, csv


# --------------------------------------------------------------------------
# envelope plot
# --------------------------------------------------------------------------

def test_plot_envelope_writes_png() -> None:
    from PIL import Image
    fps, n = 60.0, 6000
    rng = np.random.default_rng(7)
    db = np.clip(rng.normal(-50, 15, n), -80, 0)
    env = np.clip(rng.random(n), 0, 1)
    res = M.Envelope(env, -38.0, -16.0, -46.0, -20.0, True, db)
    person = M.Person(name="Test", colour=(255, 77, 166), suffix="t")
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "test_envelope.png"
        M.plot_envelope(person, res, fps, png)
        img = Image.open(png)
        assert img.size == (1600, 260)
        assert img.getpixel((0, 0)) == (24, 25, 28)   # background painted


# --------------------------------------------------------------------------
# audio decoding
# --------------------------------------------------------------------------

class _OddReader:
    """Stand-in for ffmpeg's stdout that returns odd-sized chunks, so pipe
    reads split 16-bit samples — the regression the pending-byte logic fixes."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, n: int) -> bytes:
        take = min(4097, n, len(self.data) - self.pos)
        chunk = self.data[self.pos:self.pos + take]
        self.pos += take
        return chunk


class _FakeProc:
    def __init__(self, data: bytes):
        self.stdout = _OddReader(data)

    def wait(self, timeout=None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def test_frame_loudness_db_survives_odd_chunks() -> None:
    rate = M.ANALYSIS_RATE
    t = np.arange(rate * 3) / rate
    sine = (0.25 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    real_popen = M.subprocess.Popen
    M.subprocess.Popen = lambda *a, **k: _FakeProc(sine.tobytes())
    try:
        db = M.frame_loudness_db(Path("unused.mkv"), 0, 60.0, 180)
    finally:
        M.subprocess.Popen = real_popen
    assert db.shape == (180,)
    # RMS of a 0.25-amplitude sine is 0.25/√2 ≈ -15.05 dBFS
    assert np.all(np.abs(db - (-15.05)) < 0.5), db[:5]


def test_frame_loudness_db_with_real_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        return _skip("ffmpeg not on PATH")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "sine.wav"
        M.subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3",
             "-c:a", "pcm_s16le", str(wav)], check=True)
        db = M.frame_loudness_db(wav, 0, 60.0, 180)
    # lavfi sine is fixed at 1/8 amplitude: RMS ≈ -21 dBFS
    mid = float(db[10:170].mean())
    assert -24.0 < mid < -18.0, mid


# --------------------------------------------------------------------------
# plain-python runner (no pytest needed)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback
    failed = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        fn = globals()[name]
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failed else 'passed'}"
          f" ({failed} failure(s))" if failed else "\nall tests passed")
    sys.exit(1 if failed else 0)
