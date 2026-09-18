# indicate-speaker

[![vibe coded](https://img.shields.io/badge/vibe_coded-%E2%9C%A8-ff69b4?style=flat-square)](https://en.wikipedia.org/wiki/Vibe_coding)
[![coded with Claude](https://img.shields.io/badge/coded_with-Claude_Code-CC785C?style=flat-square&logo=anthropic)](https://claude.ai/code)
[![license: AGPL v3](https://img.shields.io/badge/license-AGPL_v3-blue?style=flat-square)](LICENSE)

Run it on a finished (or half-finished) Kdenlive project of a multi-player let's play. It adds a small overlay that shows:

- **whose view we are watching**: that player's avatar is enlarged and gets a name label in their colour;
- **who is talking**: a ring in the player's colour plus a small sound-bars badge, so the cue never relies on colour alone;
- **every switch between views**: the label slides to the new player and a transition sound plays.

Everything is read from the project itself: your cuts, which clip is whose, and which audio stream is each person's voice. There are no file-name dates or stream titles to configure. The overlay fades out during the intro, the outro and other full-screen clips that aren't a player's view. It works for any game: the look comes from a small theme file with a backdrop, 1–8 avatars, colours, a font and the transition sound.

## Install and run

Needs [uv](https://docs.astral.sh/uv/). All dependencies (numpy, PyAV, skia-python, silero-vad-lite) are wheels, so nothing needs compiling and no system ffmpeg is required.

```bash
git clone … && cd indicate-speaker
uv run indicate-speaker ~/episodes/avsnitt217/avsnitt217.kdenlive --dry-run
```

| Command | What it does |
|---|---|
| `indicate-speaker P.kdenlive --dry-run` | Reports view switches, and screen and talk time per player. Writes nothing. |
| `indicate-speaker P.kdenlive --preview 90` | Renders the first 90 s to `indicate-speaker/overlay-preview.mkv`. The project is untouched. |
| `indicate-speaker P.kdenlive` | Renders the overlay and **updates the project in place**. |
| `--theme path/to/theme.toml` | Uses this theme instead of the `theme.toml` found next to or above the project. |

**Close the project in Kdenlive before running**, or at least don't save it afterwards: reopen it instead. The tool refuses to write if the project changed on disk while it worked.

## What it changes in your project

- It first saves a backup, `P.kdenlive.YYYYmmdd-HHMMSS.bak`, next to the project.
- It adds one video track (on top) and one audio track (above your other audio tracks), both named `indicate-speaker`.
- The video track holds `indicate-speaker/overlay.mkv`, spanning the whole timeline and positioned with a Transform effect. By default the file is FFV1 with alpha, which is lossless, free and open. With `codec = "qtrle"` in the theme it is `overlay.mov` instead: also lossless with alpha, about 6× smaller, but QuickTime's codec.
- The audio track holds one transition sound per view switch.
- It adds two bin clips, `indicate-speaker overlay` and `indicate-speaker sound`.

Running it again after you re-edit reuses the same tracks and clips and regenerates their contents. Nothing else in the project is touched. To take it out again, delete the two tracks and the two bin clips.

## The theme

Copy [`theme.example.toml`](theme.example.toml) to `theme.toml` next to the project, or into a folder above it such as the season folder; the nearest one wins. Keep one theme per game.

- `position` picks a corner (`top-left`, `top-right`, `bottom-left`, `bottom-right`) or an edge centre (`top`, `bottom`, `left`, `right`). `margin` is the distance from the edges the bar sits against, and `offset = [x, y]` nudges it from there in pixels (positive = right/down). For example, `position = "left"` with `offset = [0, -100]` centres the bar on the left edge, 100 px above the middle.
- `orientation = "horizontal"` (the default) draws a row with the name under the viewed avatar. `"vertical"` draws a column along the frame edge with the name beside the viewed avatar, pointing into the picture.

- `voice_track` is the name of the Kdenlive **audio track** holding that player's voice; it defaults to the player's name. The tool uses the audio stream and the cuts you chose on that track.
- Video clips from the same file as that voice track count as the player's view, whichever video track they sit on. Set `source` only when someone's view is recorded in a different file. It takes a file-name pattern such as `"*_h.mkv"`, or a list of them.
- Unknown keys get a warning (usually a typo), and wrong values stop the run with a message saying what is expected.

## How it decides

- **Whose view**: for each frame, the topmost visible video track showing a player's clip wins.
  - A full-screen clip that belongs to no player hides the overlay. That means a video or image without a Transform effect, such as the intro.
  - Placed clips (namecards, picture-in-picture) and titles don't count.
  - Views shorter than 1 s are treated as flashes: they get no switch and no sound.
- **Who talks**: [Silero VAD](https://github.com/snakers4/silero-vad) runs on each voice track, following your cuts. Two thresholds (hysteresis), a 200 ms hangover and a 120 ms minimum burst stop it from flickering on breaths and clicks.
- **Contrast**: label text is black or white, whichever contrasts more with the player's colour. That is always at least 4.5:1 (WCAG AA), so any colour is safe. The talking ring gets a black or white edge so it stays visible on any backdrop.

## Development

```bash
uv run pytest            # no media files needed
```

The code is in `indicate_speaker/`:

| File | Contents |
|---|---|
| `config.py` | Theme loading and validation |
| `timeline.py` | Reading and patching the Kdenlive XML |
| `vad.py` | Speech detection |
| `render.py` | Drawing and encoding the overlay |
| `__main__.py` | The CLI |

## License

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
