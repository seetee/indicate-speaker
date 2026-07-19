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
