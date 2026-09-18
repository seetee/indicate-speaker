"""The theme: one TOML per game, holding the look, the sound and the players."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


class VoiceError(Exception):
    """An expected failure; main() prints it as a one-line error."""


def die(msg: str):
    raise VoiceError(msg)


@dataclass(frozen=True)
class Player:
    name: str
    colour: tuple[int, int, int]
    avatar: Path
    source: str          # glob matched against clip file names, e.g. "*_h.mkv"
    voice_track: str     # Kdenlive audio track holding this player's voice


@dataclass(frozen=True)
class Theme:
    players: tuple[Player, ...]
    sound: Path
    backdrop: Path | None = None
    font: Path | None = None
    position: str = "top-left"
    margin: int = 24         # px from the frame edges
    avatar_size: int = 48    # px; the viewed player's avatar is drawn larger
    sound_offset: float = 0.0  # s; shifts every transition sound relative to its cut


def parse_colour(text: str, where: str) -> tuple[int, int, int]:
    h = str(text).lstrip("#")
    if len(h) != 6:
        die(f"{where}: colour must look like \"#e0a030\", got {text!r}")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        die(f"{where}: colour must look like \"#e0a030\", got {text!r}")


def load_theme(path: Path) -> Theme:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"theme not found: {path}")
    except tomllib.TOMLDecodeError as e:
        die(f"{path}: {e}")
    base = path.parent

    def file(value, key):
        p = (base / value).expanduser().resolve()
        if not p.is_file():
            die(f"{path.name}: {key} = {value!r}: no such file ({p})")
        return p

    players = []
    for i, p in enumerate(raw.get("player", []), 1):
        where = f"{path.name}: player #{i}"
        missing = {"name", "colour", "avatar", "source"} - p.keys()
        if missing:
            die(f"{where} is missing {', '.join(sorted(missing))}")
        players.append(Player(
            name=p["name"],
            colour=parse_colour(p["colour"], where),
            avatar=file(p["avatar"], f"{p['name']}.avatar"),
            source=p["source"],
            voice_track=p.get("voice_track", p["name"]),
        ))
    if not 1 <= len(players) <= 8:
        die(f"{path.name}: needs 1-8 [[player]] sections, found {len(players)}")
    if len({p.name for p in players}) != len(players):
        die(f"{path.name}: player names must be unique")

    if "sound" not in raw:
        die(f"{path.name}: missing sound = \"...\" (the transition sound)")
    position = raw.get("position", "top-left")
    if position not in CORNERS:
        die(f"{path.name}: position must be one of {', '.join(CORNERS)}")
    return Theme(
        players=tuple(players),
        sound=file(raw["sound"], "sound"),
        backdrop=file(raw["backdrop"], "backdrop") if "backdrop" in raw else None,
        font=file(raw["font"], "font") if "font" in raw else None,
        position=position,
        margin=int(raw.get("margin", 24)),
        avatar_size=int(raw.get("avatar_size", 48)),
        sound_offset=float(raw.get("sound_offset", 0.0)),
    )
