"""Reading and patching a Kdenlive project (MLT XML).

Only stdlib ElementTree and targeted edits: everything this tool does not own
is written back exactly as Kdenlive left it. Our additions are one video and
one audio track named TRACK_NAME, plus bin clips named CLIP_PREFIX + "...";
a re-run finds and reuses them instead of adding more.
"""

import fnmatch
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import Player, die

TRACK_NAME = "indicate-speaker"
CLIP_PREFIX = "indicate-speaker"
SUPPORTED_VERSIONS = {"1.1"}   # kdenlive:docproperties.version (Kdenlive 23.08 - 26.08)
PICTURES = {"avformat", "avformat-novalidate", "qimage", "pixbuf"}   # video and image clips
TRANSFORMS = {"qtblend", "affine"}   # an entry with one of these is placed, not full-screen


@dataclass
class Entry:
    start: int         # timeline frame
    length: int        # frames
    resource: str      # source file
    src_in: int        # first source frame used
    audio_index: int   # absolute ffmpeg stream index, -1 if none
    fullscreen: bool   # picture shown untransformed, covering the tracks below


@dataclass
class Track:
    index: int         # position among the sequence's <track>s (0 is the black background)
    name: str
    audio: bool
    hidden: bool       # video hidden / audio muted
    tractor: ET.Element
    entries: list[Entry]


@dataclass
class Project:
    tree: ET.ElementTree
    seq: ET.Element    # the sequence tractor being edited
    fps: float
    width: int
    height: int
    length: int        # frames
    tracks: list[Track]

    @property
    def root(self):
        return self.tree.getroot()

    def byid(self):
        return {e.get("id"): e for e in self.root if e.get("id")}


def props(e: ET.Element) -> dict[str, str]:
    return {p.get("name"): p.text or "" for p in e.findall("property")}


def set_prop(e: ET.Element, name: str, value) -> None:
    for p in e.findall("property"):
        if p.get("name") == name:
            p.text = str(value)
            return
    ET.SubElement(e, "property", name=name).text = str(value)


def frames(value: str | None, fps: float) -> int:
    """MLT time value -> frames. Accepts frame counts, HH:MM:SS.mmm and HH:MM:SS:FF."""
    if not value:
        return 0
    parts = value.replace(";", ":").split(":")
    if len(parts) == 1:
        return int(float(parts[0]))
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    f = int(parts[3]) if len(parts) == 4 else 0
    return round((h * 3600 + m * 60 + s) * fps) + f


def load(path: Path) -> Project:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as e:
        die(f"cannot read project {path}: {e}")
    root = tree.getroot()
    prof = root.find("profile")
    if root.tag != "mlt" or prof is None:
        die(f"{path} is not a Kdenlive project")
    fps = int(prof.get("frame_rate_num")) / int(prof.get("frame_rate_den"))
    byid = {e.get("id"): e for e in root if e.get("id")}

    bin_props = props(byid["main_bin"]) if "main_bin" in byid else {}
    version = bin_props.get("kdenlive:docproperties.version")
    if version not in SUPPORTED_VERSIONS:
        die(f"{path.name}: Kdenlive document version {version!r} is not supported "
            f"(known: {', '.join(sorted(SUPPORTED_VERSIONS))}); open and re-save it "
            "in a current Kdenlive")

    seqs = [t for t in root.iter("tractor")
            if "kdenlive:sequenceproperties.tracksCount" in props(t)]
    active = bin_props.get("kdenlive:docproperties.activetimeline")
    seq = next((t for t in seqs if props(t).get("kdenlive:uuid") == active),
               seqs[0] if len(seqs) == 1 else None)
    if seq is None:
        die(f"{path.name}: cannot tell which timeline sequence to use")

    tracks = []
    for i, ref in enumerate(seq.findall("track")):
        t = byid.get(ref.get("producer"))
        if t is None or t.tag != "tractor":
            continue                          # the black background track
        p = props(t)
        subs = t.findall("track")
        entries = []
        for sub in subs:
            pos = 0
            for c in byid[sub.get("producer")]:
                if c.tag == "blank":
                    pos += frames(c.get("length"), fps)
                elif c.tag == "entry":
                    a, b = frames(c.get("in"), fps), frames(c.get("out"), fps)
                    src = byid.get(c.get("producer"))
                    sp = props(src) if src is not None else {}
                    moved = any(props(f).get("mlt_service") in TRANSFORMS
                                for f in c.findall("filter"))
                    fullscreen = (sp.get("mlt_service") in PICTURES and not moved
                                  and sp.get("video_index") != "-1")
                    entries.append(Entry(pos, b - a + 1, sp.get("resource", ""), a,
                                         int(sp.get("audio_index") or -1), fullscreen))
                    pos += b - a + 1
        tracks.append(Track(i, p.get("kdenlive:track_name", ""),
                            p.get("kdenlive:audio_track") == "1",
                            bool(subs) and subs[0].get("hide") == "both", t, entries))
    return Project(tree, seq, fps, int(prof.get("width")), int(prof.get("height")),
                   frames(seq.get("out"), fps) + 1, tracks)


# --------------------------------------------------------------------------
# What is on screen, and whose voice is where
# --------------------------------------------------------------------------

def player_of(resource: str, players: tuple[Player, ...]) -> int | None:
    name = Path(resource).name
    return next((i for i, p in enumerate(players) if fnmatch.fnmatch(name, p.source)), None)


def view_map(project: Project, players: tuple[Player, ...]) -> np.ndarray:
    """Per frame: index of the player whose view is on screen, -1 for none.
    The topmost visible video track showing a player's clip wins. Other clips
    hide the view only when shown full-screen (an intro video, an outro card);
    placed ones (namecards, picture-in-picture) and titles are ignored."""
    view = np.full(project.length, -1, np.int16)
    for t in project.tracks:                  # ascending index = bottom to top
        if t.audio or t.hidden or t.name == TRACK_NAME:
            continue
        for e in t.entries:
            who = player_of(e.resource, players)
            if who is not None or e.fullscreen:
                view[e.start:e.start + e.length] = -1 if who is None else who
    return view


def view_spans(view: np.ndarray, min_len: int) -> list[tuple[int, int, int]]:
    """(start, end, player) runs; runs shorter than min_len frames (flash cuts,
    gaps between clips, slivers before an intro) are absorbed into the run
    before them."""
    edges = (np.flatnonzero(np.diff(view)) + 1).tolist()
    spans = [(0, 0, -1)]   # a leading "no view" run absorbs a short first run
    for s, e in zip([0, *edges], [*edges, len(view)]):
        who = int(view[s])
        if e - s < min_len or spans[-1][2] == who:
            spans[-1] = (spans[-1][0], e, spans[-1][2])
        else:
            spans.append((s, e, who))
    return [sp for sp in spans if sp[1] > sp[0]]


def cuts(spans) -> list[int]:
    """Frames where the view switches from one player to another (the swoosh
    points); the first view, e.g. after the intro, gets none."""
    out, last = [], None
    for s, _, who in spans:
        if who >= 0 and who != last:
            if last is not None:
                out.append(s)
            last = who
    return out


def voice_entries(project: Project, player: Player) -> list[Entry]:
    for t in project.tracks:
        if t.audio and t.name == player.voice_track:
            return [e for e in t.entries if e.audio_index >= 0]
    names = sorted({t.name for t in project.tracks if t.audio and t.name})
    die(f"{player.name}: no audio track named {player.voice_track!r} "
        f"(the project has: {', '.join(names)}); set voice_track in the theme")


# --------------------------------------------------------------------------
# Patching
# --------------------------------------------------------------------------

def _ensure_clip(project: Project, key: str, resource: Path, length: int,
                 audio: bool) -> tuple[str, str]:
    """Bin clip + timeline instance for one of our media files, created on first
    run and refreshed in place afterwards (so any copy of it the editor placed
    elsewhere keeps working). Found by clip name and bin membership, never by
    element id: Kdenlive renumbers ids when it saves.
    Returns (kdenlive:id, timeline producer id)."""
    root, main_bin = project.root, project.byid()["main_bin"]
    clipname = f"{CLIP_PREFIX} {key}"
    media = [e for e in root if e.tag in ("chain", "producer")]
    kid = next((props(e)["kdenlive:id"] for e in media
                if props(e).get("kdenlive:clipname") == clipname), None)
    if kid is None:
        kid = str(1 + max((int(props(e).get("kdenlive:id") or 0) for e in media), default=0))
        _new_chain(project, f"indspk_{key}_bin", kid)
        ET.SubElement(main_bin, "entry", producer=f"indspk_{key}_bin")
    in_bin = {en.get("producer") for en in main_bin.findall("entry")}
    copies = [e for e in root if e.tag in ("chain", "producer")
              and props(e).get("kdenlive:id") == kid]
    if all(e.get("id") in in_bin for e in copies):
        copies.append(_new_chain(project, f"indspk_{key}", kid))

    fields = {
        "length": length, "eof": "pause", "resource": str(resource),
        "mlt_service": "avformat-novalidate", "seekable": 1,
        "audio_index": 0 if audio else -1, "video_index": -1 if audio else 0,
        "kdenlive:clipname": clipname, "kdenlive:folderid": -1,
        "kdenlive:clip_type": 1 if audio else 2,
    }
    control = next((props(e)["kdenlive:control_uuid"] for e in copies
                    if props(e).get("kdenlive:control_uuid")), "{%s}" % uuid.uuid4())
    timeline_id = None
    for e in copies:
        for p in e.findall("property"):   # drop stale probe data (hash, meta.*)
            e.remove(p)
        e.set("out", str(length - 1))
        for k, v in {"kdenlive:id": kid, "kdenlive:control_uuid": control, **fields}.items():
            set_prop(e, k, v)
        if e.get("id") not in in_bin:
            set_prop(e, "set.test_image" if audio else "set.test_audio", 1)
            timeline_id = timeline_id or e.get("id")
    for en in main_bin.findall("entry"):
        if en.get("producer") in {e.get("id") for e in copies}:
            en.set("in", "0")
            en.set("out", str(length - 1))
    return kid, timeline_id


def _new_chain(project: Project, cid: str, kid: str) -> ET.Element:
    if cid in project.byid():
        die(f"project already has an element with id {cid!r} that is not ours")
    ch = ET.Element("chain", id=cid)
    set_prop(ch, "kdenlive:id", kid)
    # before any playlist or tractor, since MLT resolves references in file order
    at = next(i for i, e in enumerate(project.root) if e.tag in ("playlist", "tractor"))
    project.root.insert(at, ch)
    return ch


def _shift_tracks(seq: ET.Element, k: int) -> None:
    """Renumber everything in the sequence that refers to track index >= k."""
    for tr in seq.findall("transition"):
        for key in ("a_track", "b_track"):
            v = props(tr).get(key)
            if v is not None and v.isdigit() and int(v) >= k:
                set_prop(tr, key, int(v) + 1)
    # Kdenlive's own bookkeeping counts tracks without the black one
    p = props(seq)
    groups = p.get("kdenlive:sequenceproperties.groups")
    if groups:
        set_prop(seq, "kdenlive:sequenceproperties.groups", re.sub(
            r'("data":\s*")(\d+)(:)',
            lambda m: f"{m[1]}{int(m[2]) + (int(m[2]) >= k - 1)}{m[3]}", groups))
    for key in ("activeTrack", "audioTarget", "videoTarget"):
        v = p.get(f"kdenlive:sequenceproperties.{key}", "")
        if v.isdigit() and int(v) >= k - 1:
            set_prop(seq, f"kdenlive:sequenceproperties.{key}", int(v) + 1)
    n = p.get("kdenlive:sequenceproperties.tracksCount", "")
    if n.isdigit():
        set_prop(seq, "kdenlive:sequenceproperties.tracksCount", int(n) + 1)


def _ensure_track(project: Project, audio: bool) -> ET.Element:
    """Our track's first playlist, emptied; the track is created if missing."""
    seq, byid = project.seq, project.byid()
    refs = seq.findall("track")
    for ref in refs:
        t = byid.get(ref.get("producer"))
        if (t is not None and t.tag == "tractor"
                and props(t).get("kdenlive:track_name") == TRACK_NAME
                and (props(t).get("kdenlive:audio_track") == "1") == audio):
            t.set("out", str(project.length - 1))
            pls = [byid[s.get("producer")] for s in t.findall("track")]
            for pl in pls:
                for c in [c for c in pl if c.tag in ("entry", "blank")]:
                    pl.remove(c)
            return pls[0]

    base = "indspk_audio" if audio else "indspk_video"
    if base in byid:
        die(f"project already has an element with id {base!r} that is not ours")
    tractor = ET.Element("tractor", {"id": base, "in": "0", "out": str(project.length - 1)})
    if audio:
        set_prop(tractor, "kdenlive:audio_track", 1)
    set_prop(tractor, "kdenlive:track_name", TRACK_NAME)
    pls = []
    for n in range(2):
        pl = ET.Element("playlist", id=f"{base}_pl{n}")
        if audio:
            set_prop(pl, "kdenlive:audio_track", 1)
        ET.SubElement(tractor, "track", producer=pl.get("id"),
                      hide="video" if audio else "audio")
        pls.append(pl)
    at = list(project.root).index(seq)
    for n, el in enumerate([*pls, tractor]):
        project.root.insert(at + n, el)

    # audio tracks sit below all video tracks; ours goes on top of the audio block
    if audio:
        audio_refs = [r for r in refs if props(byid.get(r.get("producer"), ET.Element("x")))
                      .get("kdenlive:audio_track") == "1"]
        k = refs.index(audio_refs[-1]) + 1 if audio_refs else 1
    else:
        k = len(refs)
    _shift_tracks(seq, k)
    children = list(seq)
    seq.insert(children.index(refs[k - 1]) + 1, ET.Element("track", producer=base))

    tr = ET.Element("transition", id=f"{base}_transition")
    fields = ({"mlt_service": "mix", "kdenlive_id": "mix", "accepts_blanks": 1, "sum": 1}
              if audio else
              {"mlt_service": "qtblend", "kdenlive_id": "qtblend", "compositing": 0,
               "distort": 0, "rotate_center": 0})
    for key, v in {"a_track": 0, "b_track": k, **fields,
                   "internal_added": 237, "always_active": 1}.items():
        set_prop(tr, key, v)
    trs = seq.findall("transition")
    before = next((t for t in trs if int(props(t).get("b_track", 0)) > k), None)
    seq.insert(list(seq).index(before) if before is not None else len(seq), tr)
    return pls[0]


def patch(project: Project, overlay: Path, rect: tuple[int, int, int, int],
          sound: Path, sound_len: int, cut_frames: list[int]) -> None:
    """Put the overlay (full length, placed at rect) and one sound per cut on our tracks."""
    L = project.length
    vid, vprod = _ensure_clip(project, "overlay", overlay, L, audio=False)
    aid, aprod = _ensure_clip(project, "sound", sound, sound_len, audio=True)

    vpl = _ensure_track(project, audio=False)
    en = ET.SubElement(vpl, "entry", {"producer": vprod, "in": "0", "out": str(L - 1)})
    set_prop(en, "kdenlive:id", vid)
    f = ET.SubElement(en, "filter", id="indspk_placement")
    x, y, w, h = rect
    for k, v in {"rotate_center": 1, "mlt_service": "qtblend", "kdenlive_id": "qtblend",
                 "compositing": 0, "distort": 0, "rect": f"0={x} {y} {w} {h} 1.000000",
                 "rotation": "0=0"}.items():
        set_prop(f, k, v)

    apl = _ensure_track(project, audio=True)
    pos = 0
    for i, c in enumerate(cut_frames):
        nxt = cut_frames[i + 1] if i + 1 < len(cut_frames) else L
        n = min(sound_len, nxt - c, L - c)
        if c < pos or n <= 0:
            continue
        if c > pos:
            ET.SubElement(apl, "blank", length=str(c - pos))
        en = ET.SubElement(apl, "entry", {"producer": aprod, "in": "0", "out": str(n - 1)})
        set_prop(en, "kdenlive:id", aid)
        pos = c + n


def save(project: Project, path: Path) -> None:
    project.tree.write(path, encoding="utf-8", xml_declaration=True)
