"""The theme: one TOML per game, holding the look, the sound and the players."""

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

POSITIONS = ("top-left", "top", "top-right", "left", "right",
             "bottom-left", "bottom", "bottom-right")   # corners and edge centres
ORIENTATIONS = ("horizontal", "vertical")


class VoiceError(Exception):
    """An expected failure; main() prints it as a one-line error."""


def die(msg: str):
    raise VoiceError(msg)


@dataclass(frozen=True)
class Player:
    name: str
    colour: tuple[int, int, int]
    avatar: Path
    source: tuple[str, ...]  # globs matched against clip file names, e.g. "*_h.mkv";
                             # empty = the files used on the voice track
    voice_track: str     # Kdenlive audio track holding this player's voice


@dataclass(frozen=True)
class Theme:
    players: tuple[Player, ...]
    sound: Path
    backdrop: Path | None = None
    font: Path | None = None
    position: str = "top-left"
    offset: tuple[int, int] = (0, 0)   # px nudge after placing: +x right, +y down
    orientation: str = "horizontal"   # a row of avatars, or a column along the edge
    margin: int = 24         # px from the frame edges
    avatar_size: int = 48    # px; the viewed player's avatar is drawn larger
    sound_offset: float = 0.0  # s; shifts every transition sound relative to its cut
    codec: str = "ffv1"        # overlay video codec, see CODECS


def parse_colour(text: str, where: str) -> tuple[int, int, int]:
    h = str(text).lstrip("#")
    if len(h) != 6:
        die(f"{where}: colour must look like \"#e0a030\", got {text!r}")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        die(f"{where}: colour must look like \"#e0a030\", got {text!r}")


THEME_KEYS = {"sound", "backdrop", "font", "position", "offset", "orientation", "margin",
              "avatar_size", "sound_offset", "codec", "player"}
PLAYER_KEYS = {"name", "colour", "avatar", "source", "voice_track"}
CODECS = ("ffv1", "qtrle")   # ffv1: free/open, lossless; qtrle: ~6x smaller files


def load_theme(path: Path) -> Theme:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"theme not found: {path}")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        die(f"{path}: {e}")
    base = path.parent

    def warn_unknown(table, known, where):
        for key in sorted(table.keys() - known):
            print(f"WARNING: {where}: unknown key {key!r} (ignored; typo?)", file=sys.stderr)

    def get(table, key, kind, default, where, check=lambda v: True, rule=""):
        """table[key] if present, else default; dies unless it is a `kind` passing `check`."""
        v = table.get(key, default)
        if kind is float and type(v) is int:
            v = float(v)
        if type(v) is not kind or not check(v):
            want = rule or {str: "text", int: "a whole number", float: "a number"}[kind]
            die(f"{where}: {key} must be {want}, got {v!r}")
        return v

    def file(value, key):
        if not isinstance(value, str) or not value:
            die(f"{path.name}: {key} must be a file path, got {value!r}")
        p = (base / value).expanduser().resolve()
        if not p.is_file():
            die(f"{path.name}: {key} = {value!r}: no such file ({p})")
        return p

    warn_unknown(raw, THEME_KEYS, path.name)
    table = raw.get("player", [])
    if not isinstance(table, list):
        die(f"{path.name}: players are written as [[player]] sections")
    players = []
    for i, p in enumerate(table, 1):
        where = f"{path.name}: player #{i}"
        missing = {"name", "colour", "avatar"} - p.keys()
        if missing:
            die(f"{where} is missing {', '.join(sorted(missing))}")
        warn_unknown(p, PLAYER_KEYS, where)
        name = get(p, "name", str, "", where, lambda v: v.strip() != "", "non-empty text")
        source = p.get("source", [])
        source = [source] if isinstance(source, str) else source
        if not isinstance(source, list) or not all(isinstance(s, str) and s for s in source):
            die(f"{where}: source must be a file-name pattern or a list of them")
        players.append(Player(
            name=name,
            colour=parse_colour(p["colour"], where),
            avatar=file(p["avatar"], f"{name}.avatar"),
            source=tuple(source),
            voice_track=get(p, "voice_track", str, name, where),
        ))
    if not 1 <= len(players) <= 8:
        die(f"{path.name}: needs 1-8 [[player]] sections, found {len(players)}")
    if len({p.name for p in players}) != len(players):
        die(f"{path.name}: player names must be unique")

    if "sound" not in raw:
        die(f"{path.name}: missing sound = \"...\" (the transition sound)")
    w = path.name
    return Theme(
        players=tuple(players),
        sound=file(raw["sound"], "sound"),
        backdrop=file(raw["backdrop"], "backdrop") if "backdrop" in raw else None,
        font=file(raw["font"], "font") if "font" in raw else None,
        position=get(raw, "position", str, "top-left", w, lambda v: v in POSITIONS,
                     "one of " + ", ".join(POSITIONS)),
        offset=tuple(get(raw, "offset", list, [0, 0], w,
                         lambda v: len(v) == 2 and all(type(n) is int and abs(n) <= 4000
                                                       for n in v),
                         "[x, y] in whole pixels, e.g. [0, -40]")),
        orientation=get(raw, "orientation", str, "horizontal", w,
                        lambda v: v in ORIENTATIONS, "one of " + ", ".join(ORIENTATIONS)),
        margin=get(raw, "margin", int, 24, w, lambda v: 0 <= v <= 500, "0-500 (px)"),
        avatar_size=get(raw, "avatar_size", int, 48, w, lambda v: 16 <= v <= 256,
                        "16-256 (px)"),
        sound_offset=get(raw, "sound_offset", float, 0.0, w, lambda v: abs(v) <= 10,
                         "between -10 and 10 (s)"),
        codec=get(raw, "codec", str, "ffv1", w, lambda v: v in CODECS,
                  "one of " + ", ".join(CODECS)),
    )
