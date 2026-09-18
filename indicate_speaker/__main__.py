"""indicate-speaker: add a who-we-watch / who-talks overlay to a Kdenlive project.

    indicate-speaker PROJECT.kdenlive [--theme theme.toml] [--dry-run] [--preview SECS]

The theme is found next to the project or in any folder above it.
"""

import argparse
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import av

from . import render, timeline, vad
from .config import VoiceError, die, load_theme

MIN_SPAN = 1.0   # s; views shorter than this (flash cuts) don't count as a switch


def find_theme(project: Path) -> Path:
    for d in project.parents:
        if (d / "theme.toml").is_file():
            return d / "theme.toml"
    die(f"no theme.toml next to {project.name} or in any folder above it; "
        "pass one with --theme (see README)")


def sound_frames(path: Path, fps: float) -> int:
    try:
        with av.open(str(path)) as c:
            return max(1, math.ceil(c.duration / av.time_base * fps))
    except (av.FFmpegError, TypeError):
        die(f"cannot read the transition sound {path}")


def progress(done: int, total: int) -> None:
    end = "\n" if done >= total or not sys.stdout.isatty() else ""
    print(f"\r  rendering {100 * done // max(total, 1):3d}%", end=end, flush=True)


def run(a) -> None:
    project_path = Path(a.project).expanduser().resolve()
    theme = load_theme(Path(a.theme).expanduser().resolve() if a.theme
                       else find_theme(project_path))
    players = theme.players
    mtime = project_path.stat().st_mtime_ns if project_path.exists() else None
    proj = timeline.load(project_path)
    fps = proj.fps

    view = timeline.view_map(proj, players)
    if (view < 0).all():
        clips = sorted({Path(e.resource).name for t in proj.tracks if not t.audio
                        for e in t.entries if e.resource})
        die("no video clip matches any player's source pattern; the project's "
            f"video clips are: {', '.join(clips)}")
    spans = timeline.view_spans(view, round(MIN_SPAN * fps))
    cuts = timeline.cuts(spans)
    entries = [timeline.voice_entries(proj, p) for p in players]

    print(f"{project_path.name}: {proj.length / fps / 60:.1f} min, {len(cuts)} view switches")
    t0 = time.monotonic()
    with ThreadPoolExecutor(len(players)) as pool:
        talking = list(pool.map(lambda e: vad.talking(e, fps, proj.length), entries))
    print(f"  speech detected in {time.monotonic() - t0:.0f} s")
    for i, p in enumerate(players):
        print(f"  {p.name:<12} on screen {100 * (view == i).mean():4.1f}%   "
              f"talking {100 * talking[i].mean():4.1f}%")
    if a.dry_run:
        return

    lay = render.layout(theme, proj.width, proj.height)
    outdir = project_path.parent / "indicate-speaker"
    outdir.mkdir(exist_ok=True)
    n = proj.length if a.preview is None else min(proj.length, round(a.preview * fps))
    overlay = outdir / ("overlay-preview.mkv" if a.preview is not None else "overlay.mkv")
    render.render(theme, lay, spans, talking, fps, n, overlay, progress)
    if a.preview is not None:
        print(f"Preview written: {overlay}")
        return

    if project_path.stat().st_mtime_ns != mtime:
        die(f"{project_path.name} was saved while we worked; run again")
    backup = project_path.with_name(f"{project_path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(project_path, backup)
    shift = round(theme.sound_offset * fps)
    timeline.patch(proj, overlay, render.placement(theme, lay, proj.width, proj.height),
                   theme.sound, sound_frames(theme.sound, fps),
                   [c + shift for c in cuts if 0 <= c + shift < proj.length])
    tmp = project_path.with_name(project_path.name + ".partial")
    timeline.save(proj, tmp)
    os.replace(tmp, project_path)
    print(f"Updated {project_path.name} (backup: {backup.name}). "
          "If it is open in Kdenlive, reopen it without saving first.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="indicate-speaker", description=__doc__.split("\n")[0])
    ap.add_argument("project", help="the .kdenlive project to update in place")
    ap.add_argument("--theme", help="theme TOML (default: theme.toml next to or above the project)")
    ap.add_argument("--dry-run", action="store_true", help="analyse and report; write nothing")
    ap.add_argument("--preview", type=float, metavar="SECS",
                    help="render only the first SECS to overlay-preview.mkv; project untouched")
    a = ap.parse_args(argv)
    try:
        run(a)
    except VoiceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
