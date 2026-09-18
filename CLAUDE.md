# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`indicate-speaker` (AGPL-3.0-or-later) reads a Kdenlive project of a multi-player let's play and patches it **in place**, after a timestamped `.bak`. It adds one video track and one audio track, both named `indicate-speaker`:
- The video track holds a transparent overlay: a backdrop with 1–8 player avatars, the viewed player enlarged with a name label, and a talking marker on whoever speaks.
- The audio track holds a transition sound at every switch between players' views.

The look comes from a per-game `theme.toml` (see `theme.example.toml`), found next to or above the project.

The project file is the single input. Cuts, clip-to-player mapping (by source file name; by default the files on the player's voice track, see `with_sources`) and each voice's audio stream (`audio_index` on the voice track's clips) all come from it. Don't reintroduce file-name/date/stream-title matching.

## Commands

```bash
uv run indicate-speaker P.kdenlive --dry-run      # analyse only; prints switches + screen/talk %
uv run indicate-speaker P.kdenlive --preview 90   # first 90 s -> indicate-speaker/overlay-preview.mkv
uv run indicate-speaker P.kdenlive                # render + patch the project
uv run pytest                                     # single test: -k <name>; no media needed
```

Test end to end only on a **copy** of a real project. The user's projects and Kdenlive backups live under `~/documents/samkvam.online/…` and `~/.local/share/kdenlive/.backup/`.

## Layout

- `config.py`: `Theme`/`Player` dataclasses and `load_theme`. Every value is type- and range-checked, and unknown keys warn (`THEME_KEYS`/`PLAYER_KEYS`: add new keys there). Every expected failure goes through `die()` → `VoiceError`, printed once by `main()` as `ERROR: …`. `timeline.load` also converts structural surprises (`KeyError` and similar) into `die()`.
- `timeline.py`: stdlib ElementTree only, with targeted edits. Everything the tool doesn't own must round-trip untouched.
  - `load` finds the active sequence tractor (via `docproperties.activetimeline`) and turns playlists into `Entry`s (timeline start, length, resource, src_in, audio_index, fullscreen).
  - `view_map`: the topmost visible video track showing a player's clip wins. A full-screen non-player clip (avformat/qimage without a `qtblend`/`affine` filter) means "no view".
  - `view_spans` absorbs runs shorter than `MIN_SPAN`.
  - `cuts` only counts player→player switches.
  - `patch` reuses or creates our tracks and bin clips (bin clips are identified by `kdenlive:clipname` prefix `indicate-speaker`), so re-runs are byte-identical.
  - Inserting a track must renumber `a_track`/`b_track` on the sequence's transitions, the track indices in the `sequenceproperties.groups` JSON, and `activeTrack`/`audioTarget`/`videoTarget`. Those Kdenlive indices exclude the black track, so they are `seq index - 1`. Also bump `tracksCount`.
- `vad.py`: PyAV decodes each voice entry (seek, then resample to 16 kHz mono). `silero-vad-lite` scores 512-sample windows; `smooth()` applies hysteresis, hangover and minimum burst. There is no loudness gate or normalisation: quiet mics are fine for Silero.
- `render.py`:
  - skia draws each frame; PyAV encodes per `CODECS`: theme `codec` is `ffv1` (default, `.mkv`, `bgra`) or `qtrle` (`.mov`, `argb`). Alpha is load-bearing: never add a codec without checking that alpha survives in Kdenlive (VP8/VP9 WebM alpha doesn't). `test_render_is_lossless_with_alpha` round-trips both.
  - `Painter` is built before speech analysis so bad avatars, backdrops or fonts fail fast. `load_image`/`load_font` turn skia failures into `die()`.
  - The canvas always has the **profile's aspect ratio**, so MLT maps it 1:1 onto a qtblend `rect` of the same size (verified with melt). Don't make it a different aspect.
  - Label text colour is `on_colour()` (black or white): the better of the two is always ≥ 4.5:1, so no colour ever needs adjusting.
  - Identical frame states are cached.

## Verifying against real Kdenlive

`melt` is not on PATH. Extract it from the AppImage (`~/bin/gearlever_kdenlive_*.appimage --appimage-extract`), source `apprun-hooks/craft-runenv-hook.sh`, and set `LD_LIBRARY_PATH=squashfs-root/usr/lib`. To render the timeline rather than `main_bin`, point the root `producer` attribute at the `kdenlive:projectTractor` tractor. `AppRun --render P.kdenlive out.mp4` (with `QT_QPA_PLATFORM=offscreen`) loads the project through Kdenlive's own document model.
