# indicate-speaker

[![vibe coded](https://img.shields.io/badge/vibe_coded-%E2%9C%A8-ff69b4?style=flat-square)](https://en.wikipedia.org/wiki/Vibe_coding)
[![coded with Claude](https://img.shields.io/badge/coded_with-Claude_Code-CC785C?style=flat-square&logo=anthropic)](https://claude.ai/code)
[![license: AGPL v3](https://img.shields.io/badge/license-AGPL_v3-blue?style=flat-square)](LICENSE)

Per-player "who is speaking" overlays for Minecraft let's-play editing. Each player gets an animated avatar head that reacts to their voice: it lights up, scales, blooms, and shows corner accents while they talk — and breathes slowly while they are quiet.

<p align="center">
  <img src="indicate-speaker_preview.png" alt="Quiet vs speaking states for all four players" width="280">
</p>

Output is one transparent `.mov` overlay per person. Drop them above your footage in Kdenlive (or any NLE that handles QuickTime alpha), align them to their matching source clip once, and you are done.

---

## Features

- **Spring-physics animation** — the head snaps to attention with a natural overshoot rather than a dimmer-switch fade
- **Multi-layer bloom** — three concentric glow layers (tight core → mid halo → wide ambient) give a luminous, backlit look
- **Corner accents** — L-shaped bracket marks frame the head while speaking, HUD-style
- **Idle breathing** — a slow sinusoidal pulse keeps the indicator visually alive when someone is quiet
- **Per-person normalisation** (`--normalize`) — equalises activation across mismatched mic levels so a quiet mic lights up as strongly as a loud one
- **Interactive track discovery** (`--discover`) — lists the audio streams in each MKV and saves your choices back to the config
- **Tight canvas** (default) — encodes only the sprite, ~24× faster and ~3× smaller than a full 1920×1080 frame; position is set once in Kdenlive and saved as an effect favourite
- **Parallel rendering** (`--jobs N`) — processes all players simultaneously on multi-core machines
- **Contact sheet** (`--contact-sheet`) — renders a quiet-vs-speaking PNG preview before you commit to a full encode

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | 3.11+ | Uses `tomllib` (stdlib) |
| [NumPy](https://numpy.org/) | any recent | Audio analysis and per-frame compositing |
| [Pillow](https://python-pillow.org/) | any recent | Sprite rendering |
| [FFmpeg](https://ffmpeg.org/) | any recent | Audio decode and video encode — must be on `PATH` |

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/seetee/indicate-speaker.git
cd indicate-speaker

# 2. Install Python dependencies
pip install numpy Pillow

# 3. Verify FFmpeg is available
ffmpeg -version
```

No build step, no package to install — it is a single script.

---

## Quick start

```bash
# Preview the look of all heads (no MKV files needed)
python3 indicate-speaker.py --contact-sheet

# First time with a new recording setup: identify voice tracks interactively
python3 indicate-speaker.py --discover --indir /path/to/episode

# Full render, all four players in parallel
python3 indicate-speaker.py --indir /path/to/episode --jobs 4

# Check audio analysis without rendering anything
python3 indicate-speaker.py --indir /path/to/episode --dry-run

# Render only the first 30 seconds for a quick look
python3 indicate-speaker.py --indir /path/to/episode --preview 30
```

The config file (`indicate-speaker.toml`) is found automatically when it lives next to the script, so you do not need to pass it on the command line.

---

## Usage

```
python3 indicate-speaker.py [CONFIG.toml] [options]
```

If `CONFIG.toml` is omitted the script looks for `indicate-speaker.toml` next to itself, then for the only `.toml` file in the current directory.

### Options

| Flag | Default | Description |
|---|---|---|
| `--indir DIR` | config's folder | Folder containing the episode's MKV files |
| `--outdir DIR` | same as `--indir` | Where to write the overlay `.mov` files |
| `--date YYYY-MM-DD` | auto-detect | Disambiguate when a folder holds multiple episodes |
| `--jobs N` / `-j N` | `1` | Render N players in parallel |
| `--canvas tight\|full` | `tight` | `tight`: sprite only (fast, small). `full`: 1920×1080 frame (drop straight on track) |
| `--person NAME` | all | Only process this person; repeatable |
| `--discover` | off | Interactively pick each person's voice track; saves choices to config |
| `--normalize` | off | Derive loudness thresholds per-person so quiet mics activate equally |
| `--contact-sheet` | off | Write a PNG preview of all heads and exit |
| `--refresh-heads` | off | Re-download avatar heads instead of using cached copies |
| `--preview SECONDS` | off | Only process the first N seconds |
| `--dry-run` | off | Analyse audio and report speaking time; render nothing |

### Source file naming

Each MKV must be named `YYYY-MM-DD_<suffix>.mkv`, where `<suffix>` matches the `suffix` field in the config. For example, a config with `suffix = "k"` and `--date 2026-06-27` looks for `2026-06-27_k.mkv`.

If the folder contains exactly one episode (one file per suffix), `--date` can be omitted.

### Kdenlive workflow

1. **Tight canvas** (default): after importing an overlay, add a **Transform** effect, set *Size* to the sprite's native pixel dimensions and *Position* to the `X=… Y=…` values printed by the script. Save it as an effect favourite to re-apply in one click.
2. Align the overlay to its matching source by waveform (mute the overlay audio track afterwards — it is there only to aid sync).
3. Group the overlay with its source clip so they stay together when you cut.
4. Optionally select all overlays and create a **Sequence** so they become one tidy, cuttable object that never shifts relative to each other.

---

## Configuration reference

All settings live in a TOML file (default: `indicate-speaker.toml`). Every field listed below has a built-in default; only include the lines you want to override.

### `[project]`

```toml
[project]
width        = 1920
height       = 1080
fps          = 60
stream_title = "Voice audio"   # audio stream title to use for all players
```

`stream_title` is the project-level default. Individual `[[person]]` sections can override it. Use `--discover` to find the right value automatically.

### `[input]`

```toml
[input]
dir  = "/path/to/episode"   # equivalent to --indir
date = "2026-06-27"         # equivalent to --date
```

### `[layout]`

Controls the visual appearance of the overlay.

```toml
[layout]
head_size     = 56     # head size in px at 100% activation (speaking)
gap           = 8      # vertical gap between heads, px
margin_top    = 12     # px from the top edge of the canvas
margin_left   = 12     # px from the left edge of the canvas
silent_scale  = 0.88   # head scale when fully quiet (1.0 = same as speaking)
silent_dim    = 0.78   # head brightness/alpha when fully quiet
glow_strength = 0.55   # peak bloom opacity (scales all three glow layers)
ring_width    = 3      # thickness of the rounded-rectangle ring, px
breath_freq   = 0.40   # Hz; idle breathing pulse rate (lower = slower)
breath_scale  = 0.06   # ±fraction brightness swing while silent (0 = off)
```

### `[gate]`

Controls how loudness is mapped to the 0–1 speaking activation.

```toml
[gate]
open_db              = -38.0  # activation starts rising above this dBFS
full_db              = -16.0  # activation hits 1.0 at/above this dBFS
close_db             = -46.0  # below this, forced to silence (ignores room tone)
spring_stiffness     = 400.0  # k; higher = faster snap to speaking
spring_damping_ratio = 0.65   # ζ; 1.0 = no overshoot, 0.4 = noticeably bouncy
normalize            = false  # true = derive open/full/close per-person from their track
norm_low_pct         = 15.0   # percentile of active frames mapped to open_db
norm_high_pct        = 90.0   # percentile of active frames mapped to full_db
```

**`normalize`** is useful when players have significantly different mic levels: it measures each person's own loudness distribution (noise floor and speech peak) and derives all three thresholds from it, so a quieter mic lights up the indicator just as strongly as a loud one — even a mic recorded tens of dB below the configured thresholds. Can also be enabled per-run with `--normalize`.

Any `[gate]` value can also be set inside a `[[person]]` section to override it for that person only — handy for one unusually quiet or loud mic.

The script also warns when something looks wrong with the audio: when a person's loudness never reaches the gate (the head would stay dark for the whole episode), and when another stream — typically game audio — appears to bleed into the chosen voice track (checked by correlating the streams over a sample from the middle of the recording). All warnings are repeated in a summary at the end of the run, so they are not lost between progress lines when rendering with `--jobs`.

### `[[person]]`

One section per player, in the order they should appear top-to-bottom on screen.

```toml
[[person]]
name         = "Kenneth"
suffix       = "k"              # matches YYYY-MM-DD_k.mkv
nick         = "seetee"         # Minecraft username; fetches head from mc-heads.net
colour       = "#ff4da6"        # ring, glow, and corner accent colour
# head_file  = "heads/k.png"   # use a local PNG instead of mc-heads.net
# stream_title = "Voice audio" # overrides [project] stream_title for this person
# open_db    = -60.0           # any [gate] value can be overridden per person
```

`nick` and `head_file` are mutually exclusive; at least one is required. Downloaded heads are cached in `~/.cache/indicate-speaker/`, so renders work offline after the first fetch; pass `--refresh-heads` after someone changes their skin. Run `--discover` once after a new recording session if the audio track names have changed — it will find the right `stream_title` for each player and write it into the config.

---

## License

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
