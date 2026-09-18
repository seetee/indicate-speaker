"""uv run pytest   (no media files needed: a hand-built project mirrors avsnitt217's structure)"""

import itertools
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import numpy as np

from indicate_speaker import render, timeline as T, vad
from indicate_speaker.config import Player

PLAYERS = tuple(Player(n, (200, 60, 90), Path("x.png"), (f"*_{n[0].lower()}.mkv",), n)
                for n in ("Henrik", "Kenneth"))


def chain(cid, kid, resource, audio_index=-1, service="avformat-novalidate", video_index=0):
    return (f'<chain id="{cid}" out="99999"><property name="resource">{resource}</property>'
            f'<property name="mlt_service">{service}</property>'
            f'<property name="audio_index">{audio_index}</property>'
            f'<property name="video_index">{video_index}</property>'
            f'<property name="kdenlive:id">{kid}</property></chain>')


def track(tid, name, audio, body, hide=None):
    hide = hide or ("video" if audio else "audio")
    at = '<property name="kdenlive:audio_track">1</property>' if audio else ""
    return (f'<playlist id="{tid}_a">{at}{body}</playlist><playlist id="{tid}_b">{at}</playlist>'
            f'<tractor id="{tid}" in="0" out="999">{at}'
            f'<property name="kdenlive:track_name">{name}</property>'
            f'<track hide="{hide}" producer="{tid}_a"/><track hide="{hide}" producer="{tid}_b"/></tractor>')


# frames at 10 fps: Kenneth's clip sits on the track named "Henrik" (as in avsnitt217)
PROJECT = f"""<?xml version='1.0' encoding='utf-8'?>
<mlt LC_NUMERIC="C" producer="main_bin" version="7.40.0">
 <profile frame_rate_num="10" frame_rate_den="1" width="1920" height="1080"/>
 {chain("c_h", 1, "/src/2025-08-23_h.mkv", 2)}
 {chain("c_k", 2, "/src/2025-08-23_k.mkv", 2)}
 {chain("c_intro", 3, "/a/intro.mov")}
 {chain("c_card", 4, "/a/card.png", service="qimage")}
 <producer id="black"><property name="mlt_service">color</property></producer>
 {track("t_voice", "Henrik", True, '<entry producer="c_h" in="100" out="299"/>')}
 {track("t_vh", "Henrik", False,
        '<entry producer="c_h" in="0" out="99"/><entry producer="c_k" in="0" out="49"/>'
        '<entry producer="c_h" in="0" out="3"/><entry producer="c_k" in="0" out="45"/>')}
 {track("t_top", "Assets", False,
        '<entry producer="c_intro" in="0" out="19"/><blank length="100"/>'
        '<entry producer="c_card" in="0" out="29"><filter><property name="mlt_service">qtblend</property></filter></entry>')}
 <tractor id="seq" in="0" out="199">
  <property name="kdenlive:uuid">{{u}}</property>
  <property name="kdenlive:sequenceproperties.tracksCount">3</property>
  <property name="kdenlive:sequenceproperties.videoTarget">1</property>
  <property name="kdenlive:sequenceproperties.groups">[{{"data": "0:10:-1"}}, {{"data": "2:50:-1"}}]</property>
  <track producer="black"/><track producer="t_voice"/><track producer="t_vh"/><track producer="t_top"/>
  <transition id="x1"><property name="a_track">0</property><property name="b_track">1</property><property name="mlt_service">mix</property></transition>
  <transition id="x2"><property name="a_track">0</property><property name="b_track">2</property><property name="mlt_service">qtblend</property></transition>
  <transition id="x3"><property name="a_track">0</property><property name="b_track">3</property><property name="mlt_service">qtblend</property></transition>
 </tractor>
 <playlist id="main_bin">
  <property name="kdenlive:docproperties.version">1.1</property>
  <property name="kdenlive:docproperties.activetimeline">{{u}}</property>
  <entry producer="c_h"/><entry producer="seq"/>
 </playlist>
 <tractor id="project"><track producer="seq"/></tractor>
</mlt>
"""


def load(tmp_path):
    p = tmp_path / "ep.kdenlive"
    p.write_text(PROJECT)
    return T.load(p)


def test_parse_and_view(tmp_path):
    proj = load(tmp_path)
    assert (proj.fps, proj.length) == (10, 200)
    assert [(t.index, t.name, t.audio) for t in proj.tracks] == [
        (1, "Henrik", True), (2, "Henrik", False), (3, "Assets", False)]
    view = T.view_map(proj, PLAYERS)
    assert (view[:20] == -1).all()              # full-screen intro covers Henrik
    assert (view[20:100] == 0).all()
    assert (view[100:150] == 1).all()           # identity comes from the clip, not the track name
    assert (view[120:150] == 1).all()           # a placed card (qtblend) does not cover
    spans = T.view_spans(view, 10)              # the 4-frame Henrik flash is absorbed
    assert spans == [(0, 20, -1), (20, 100, 0), (100, 200, 1)]
    assert T.cuts(spans) == [100]               # no swoosh for the first view after the intro
    assert [(e.start, e.src_in, e.audio_index) for e in T.voice_entries(proj, PLAYERS[0])] == [
        (0, 100, 2)]


def test_source_defaults_to_the_voice_tracks_files(tmp_path):
    proj = load(tmp_path)
    bare = (replace(PLAYERS[0], source=()), PLAYERS[1])
    players = T.with_sources(proj, bare)
    assert players[0].source == ("2025-08-23_h.mkv",)
    assert (T.view_map(proj, players) == T.view_map(proj, PLAYERS)).all()


def test_patch_adds_tracks_once_and_renumbers(tmp_path):
    proj = load(tmp_path)
    T.patch(proj, tmp_path / "overlay.mkv", (24, 24, 304, 171), tmp_path / "s.flac", 25, [100, 190])
    out = tmp_path / "out.kdenlive"
    T.save(proj, out)
    first = out.read_bytes()

    seq = T.load(out).seq
    refs = [r.get("producer") for r in seq.findall("track")]
    assert refs == ["black", "t_voice", "indspk_audio", "t_vh", "t_top", "indspk_video"]
    b = {t.get("id"): T.props(t)["b_track"] for t in seq.findall("transition")}
    assert b == {"x1": "1", "indspk_audio_transition": "2", "x2": "3", "x3": "4",
                 "indspk_video_transition": "5"}
    p = T.props(seq)
    assert p["kdenlive:sequenceproperties.groups"] == '[{"data": "0:10:-1"}, {"data": "3:50:-1"}]'
    assert p["kdenlive:sequenceproperties.tracksCount"] == "5"
    assert p["kdenlive:sequenceproperties.videoTarget"] == "2"

    root = ET.parse(out).getroot()
    byid = {e.get("id"): e for e in root if e.get("id")}
    sounds = byid["indspk_audio_pl0"].findall("entry")
    assert [(e.get("in"), e.get("out")) for e in sounds] == [("0", "24"), ("0", "9")]  # last one trimmed
    assert byid["indspk_audio_pl0"].find("blank").get("length") == "100"
    rect = [T.props(f)["rect"] for f in byid["indspk_video_pl0"].iter("filter")]
    assert rect == ["0=24 24 304 171 1.000000"]

    again = T.load(out)                        # a re-run reuses everything we own
    T.patch(again, tmp_path / "overlay.mkv", (24, 24, 304, 171), tmp_path / "s.flac", 25, [100, 190])
    T.save(again, out)
    assert out.read_bytes() == first


def test_label_text_contrast_always_passes_wcag_aa():
    for rgb in itertools.product(range(0, 256, 17), repeat=3):
        assert render.contrast(rgb, render.on_colour(rgb)) >= 4.5


def test_canvas_matches_frame_aspect():
    from indicate_speaker.config import Theme
    for n, orientation in itertools.product((1, 4, 8), ("horizontal", "vertical")):
        theme = Theme(players=PLAYERS[:1] * n, sound=Path("s.flac"), position="bottom-right",
                      orientation=orientation)
        lay = render.layout(theme, 1920, 1080)
        assert lay.W * 1080 == lay.H * 1920
        assert lay.panel.width() <= lay.W and lay.panel.height() <= lay.H
        assert all(lay.panel.contains(x, y) for x, y in lay.slots)
        x, y, w, h = render.placement(theme, lay, 1920, 1080)
        assert (x + w, y + h) == (1920 - theme.margin, 1080 - theme.margin)


def test_vad_smoothing():
    hang = round(vad.HANGOVER * vad.RATE / vad.WINDOW)
    probs = np.zeros(100)
    probs[10:40] = 0.9          # speech
    probs[40:43] = 0.4          # dip between the thresholds: stays on
    probs[43:45] = 0.9
    probs[70:71] = 0.9          # a click: shorter than MIN_BURST once the hangover is discounted
    on = vad.smooth(probs)
    assert on[10:45 + hang - 1].all() and not on[45 + hang:].any()
    assert not on[:10].any()


def test_rerun_after_kdenlive_renamed_our_ids(tmp_path):
    proj = load(tmp_path)
    T.patch(proj, tmp_path / "overlay.mkv", (24, 24, 304, 171), tmp_path / "s.flac", 25, [100])
    out = tmp_path / "out.kdenlive"
    T.save(proj, out)
    out.write_text(out.read_text().replace("indspk_", "kd"))   # Kdenlive renumbers ids on save

    again = T.load(out)
    T.patch(again, tmp_path / "overlay.mkv", (24, 24, 304, 171), tmp_path / "s.flac", 30, [100])
    T.save(again, out)
    root = ET.parse(out).getroot()
    byid = {e.get("id"): e for e in root if e.get("id")}
    names = [T.props(t).get("kdenlive:track_name") for t in root.iter("tractor")]
    assert names.count("indicate-speaker") == 2
    ours = [e for e in root if e.tag == "chain"
            and T.props(e).get("kdenlive:clipname", "").startswith("indicate-speaker")]
    assert len(ours) == 4                                  # bin + timeline copy, twice
    sound_entries = [e for pl in root.iter("playlist") for e in pl.findall("entry")
                     if T.props(byid[e.get("producer")]).get("kdenlive:clipname") == "indicate-speaker sound"]
    assert [e.get("out") for e in sound_entries] == ["29", "29"]   # bin entry + the one sound, both refreshed


# --- theme, assets and robustness --------------------------------------------

import pytest
import skia

from indicate_speaker import __main__ as cli
from indicate_speaker.config import VoiceError, load_theme


def write_theme(tmp_path, extra="", player_extra=""):
    for name in ("a.png", "s.flac"):
        (tmp_path / name).write_bytes(b"x")
    avatar = skia.Surface(8, 8)
    avatar.getCanvas().clear(skia.ColorRED)
    avatar.makeImageSnapshot().save(str(tmp_path / "a.png"))
    theme = tmp_path / "theme.toml"
    theme.write_text(f'sound = "s.flac"\n{extra}\n[[player]]\nname = "Kenneth-the-long"\n'
                     f'colour = "#c83c5a"\navatar = "a.png"\n{player_extra}\n')
    return theme


@pytest.mark.parametrize("extra, message", [
    ("margin = \"abc\"", "margin must be 0-500"),
    ("avatar_size = 4", "avatar_size must be 16-256"),
    ("codec = \"vp9\"", "codec must be one of ffv1, qtrle"),
    ("orientation = true", "orientation must be one of"),
])
def test_bad_theme_values_are_clear_errors(tmp_path, extra, message):
    with pytest.raises(VoiceError, match=message):
        load_theme(write_theme(tmp_path, extra))


def test_theme_warns_about_typos_and_takes_source_lists(tmp_path, capsys):
    theme = load_theme(write_theme(tmp_path, 'orientaton = "vertical"',
                                   'source = ["*_k.mkv", "*_k2.mkv"]'))
    assert "unknown key 'orientaton'" in capsys.readouterr().err
    assert theme.orientation == "horizontal" and theme.players[0].source == ("*_k.mkv", "*_k2.mkv")


def test_unreadable_assets_are_clear_errors(tmp_path):
    theme = load_theme(write_theme(tmp_path, 'font = "s.flac"'))
    with pytest.raises(VoiceError, match="not a font file"):
        render.layout(theme, 1920, 1080)
    theme = load_theme(write_theme(tmp_path, 'backdrop = "s.flac"'))
    with pytest.raises(VoiceError, match="backdrop .* is not an image"):
        render.Painter(theme, render.layout(theme, 1920, 1080))


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_long_name_fits_inside_the_panel(tmp_path, orientation):
    theme = load_theme(write_theme(tmp_path, f'orientation = "{orientation}"'))
    lay = render.layout(theme, 1920, 1080)
    width = render.load_font(theme, lay.font_size).measureText("Kenneth-the-long") + lay.label_h
    assert width + 8 <= lay.panel.width() <= lay.W


@pytest.mark.parametrize("codec", ["ffv1", "qtrle"])
def test_render_is_lossless_with_alpha(tmp_path, codec):
    import av
    theme = load_theme(write_theme(tmp_path, f'codec = "{codec}"'))
    painter = render.Painter(theme, render.layout(theme, 1920, 1080))
    out = tmp_path / ("o" + render.CODECS[codec][0])
    render.render(painter, codec, [(0, 12, 0)], [np.r_[np.zeros(6, bool), np.ones(6, bool)]],
                  30.0, 12, out)
    with av.open(str(out)) as c:
        frames = [f.to_ndarray(format="bgra") for f in c.decode(video=0)]
    assert len(frames) == 12 and not (tmp_path / (out.name + ".partial")).exists()
    want = painter.draw(1.0, -1, 0, 1.0, (1.0,))       # last frame: fully faded in, talking
    assert (frames[-1] == want).all()
    assert frames[-1][..., 3].min() == 0 and frames[-1][..., 3].max() == 255   # real alpha


def test_new_clip_ids_avoid_sequence_ids(tmp_path):
    p = tmp_path / "ep.kdenlive"
    p.write_text(PROJECT.replace('<tractor id="seq" in="0" out="199">',
                                 '<tractor id="seq" in="0" out="199">'
                                 '<property name="kdenlive:id">40</property>'))
    proj = T.load(p)
    T.patch(proj, tmp_path / "o.mkv", (0, 0, 16, 9), tmp_path / "s.flac", 5, [100])
    ids = {T.props(e).get("kdenlive:clipname"): T.props(e).get("kdenlive:id")
           for e in proj.root if e.tag == "chain"}
    assert ids["indicate-speaker overlay"] == "41" and ids["indicate-speaker sound"] == "42"


def test_malformed_project_is_a_clear_error(tmp_path):
    p = tmp_path / "ep.kdenlive"
    p.write_text(PROJECT.replace('<track hide="audio" producer="t_vh_a"/>',
                                 '<track hide="audio" producer="missing"/>'))
    with pytest.raises(VoiceError, match="unexpected project structure"):
        T.load(p)


def test_preview_must_be_positive():
    assert cli.positive("1.5") == 1.5
    for bad in ("0", "-3", "abc"):
        with pytest.raises(Exception, match="above 0"):
            cli.positive(bad)
