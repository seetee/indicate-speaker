# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python tool (`indicate-speaker.py`, AGPL-3.0-or-later) that renders per-player "who is speaking" overlay videos for Minecraft let's-play editing. For each person in the config it reads their voice track from an MKV, converts loudness into a 0–1 speaking activation, and encodes a transparent QuickTime Animation (qtrle) `.mov` where their Minecraft avatar head lights up, scales, and glows while they talk.

There is no build step or linter. Dependencies: Python 3.11+ (uses stdlib `tomllib`), `numpy`, `Pillow`, and `ffmpeg`/`ffprobe` on PATH; the script carries PEP 723 inline metadata, so `uv run indicate-speaker.py` works with zero setup.

Tests live in `tests/test_indicate_speaker.py` and run two ways (the system Python has no pip/pytest — use uv for pytest):

```bash
python3 tests/test_indicate_speaker.py                            # no extra deps
uv run --with pytest --with numpy --with pillow -m pytest tests/  # single test: -k <name>
```

ffmpeg-dependent tests self-skip when ffmpeg is absent. The module has a hyphenated filename, so tests load it via `importlib` with a `sys.modules["indspk"]` registration (required for dataclass processing) — reuse that pattern for any new test file.

## Common commands

```bash
# Fast visual check of sprite rendering — needs no MKV files
python3 indicate-speaker.py --contact-sheet

# Analyse audio and report speaking time without rendering (fast-ish sanity check)
python3 indicate-speaker.py --indir /path/to/episode --dry-run

# Render only the first N seconds — the quickest end-to-end test of a change
python3 indicate-speaker.py --indir /path/to/episode --preview 30

# Full render, one job per person
python3 indicate-speaker.py --indir /path/to/episode --jobs 4

# Restrict to one person (repeatable)
python3 indicate-speaker.py --indir /path/to/episode --person Kenneth

# Interactively identify voice tracks after a recording-setup change;
# writes stream_title choices back into the TOML (requires a TTY)
python3 indicate-speaker.py --discover --indir /path/to/episode
```

The config (`indicate-speaker.toml` next to the script) is found automatically; source MKVs are matched as `YYYY-MM-DD_<suffix>.mkv` in `--indir`, with `--date` only needed when a folder holds multiple episodes.

## Architecture

Everything lives in `indicate-speaker.py`, organised as a pipeline that runs once per person (optionally in parallel via `ThreadPoolExecutor`; a shared `threading.Event` aborts sibling jobs when one fails):

1. **Config** (`load_config`): TOML → `Layout`, `Gate`, and `Person` dataclasses. Any `[gate]` key can be overridden inside a `[[person]]` section — each `Person` carries its own merged `Gate` (`replace(gate, **overrides)`), so use `person.gate`, not the global, when rendering.
2. **Source resolution** (`find_episode_dir` + `resolve_sources`): the input dir defaults to the **current working directory** (the tool is normally run from inside an episode's `sources/` folder). When that dir doesn't hold the MKVs itself, the newest complete episode up to two levels below it is auto-selected; when nothing is found, `ask_episode_dir` prompts interactively for the sources directory (dies when not a TTY). `resolve_sources` then fills `person.source` from suffix + date/glob. Audio streams are matched **by stream title** (`find_audio_index`), not index — OBS track order is unreliable, titles are the contract.
3. **Audio analysis** (`frame_loudness_db`): decodes the voice stream via ffmpeg to raw s16le at 8 kHz mono and bins RMS per video frame. Deliberately single-pass: the same ffmpeg invocation also writes the voice as a small AAC file (`voice_out`) that later gets muxed (`-c:a copy`) into the overlay for waveform sync in the NLE — do not add a second read of the (large) source.
4. **Envelope** (`activation_envelope`): maps dBFS to 0–1 activation through open/full/close gate thresholds, then smooths it with a second-order mass-spring-damper (semi-implicit Euler; underdamped for overshoot on attack). `normalize` is three-valued (`"auto"`/`true`/`false`, default `"auto"`): normalization derives all three thresholds — including `close_db` — from that person's own loudness distribution, anchored to the *peak* (not the floor) so noise-gated mics that record silence as −180 dB still work; in `"auto"` mode it is applied only when the fixed gate demonstrably fails the track (`_gate_failed`), re-running the cheap in-memory gating, not the ffmpeg decode. Overshoot above 1.0 is intentionally not clamped in the envelope; the render loop clips to `LEVELS - 1`.
5. **Sprites** (`render_sprites`): activation is quantised to `LEVELS = 64` pre-rendered RGBA sprite states (head + multi-layer bloom + ring + corner accents). The per-frame loop writes pre-converted bytes: full-activation frames come from `sprite_bytes` untouched, and breathing frames quantise the brightness factor to 1/256 (invisible at 8-bit output) and memoise the result (`frame_cache`, capped at `FRAME_CACHE_MAX`), so recurring states are dict lookups rather than array math.
6. **Encode** (`render_overlay` / `build_render_cmd`): frames are streamed as rawvideo into ffmpeg's stdin, encoded as **qtrle** in `.mov`. This codec choice is load-bearing: VP9/VP8 alpha WebM loses its alpha in the FFmpeg backend Kdenlive uses, so do not "upgrade" it. Two canvas modes: `tight` (default, sprite-sized; Kdenlive position printed as `X=… Y=…`) and `full` (1920×1080 pre-positioned). Output goes to a `.partial` temp name and is `os.replace`d on success.

Cross-cutting conventions:

- All expected failures go through `die()` → `VoiceError`, caught once in `main()` and printed as `ERROR: …`. Don't call `sys.exit` or print errors from within the pipeline.
- Audio-quality diagnostics are warnings, not errors: `_warn_weak_signal` (gate can never open / almost no speech detected) and `check_track_bleed` (correlates sibling streams over a middle-of-recording window to catch OBS routing game audio onto the mic track; deliberately skipped on `--preview` runs, where its full-file decode would dominate).
- `--discover` patches the TOML **textually** via regex (`patch_config_stream_titles`) to preserve the user's comments and formatting — don't replace this with a TOML serializer round-trip.
- Progress output adapts: live `\r`-overwriting lines only when single-job and on a TTY; periodic plain lines otherwise (`Progress`).
