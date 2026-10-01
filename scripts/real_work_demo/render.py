"""Render the companion demo from an evidence-backed storyboard JSON.

uv run --locked --with pillow python -m scripts.real_work_demo.render STORY OUTPUT
"""

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import (  # type: ignore[import-not-found]  # rendering extra: uv run --with pillow
    Image,
    ImageDraw,
    ImageFont,
)

Box = tuple[int, int, int, int]

W, H = 1920, 1080
# Palette taken from the Cairn hero image (docs/assets/hero-dark.png).
BG = "#131b41"  # night sky
VIOLET = "#50296d"  # upper sky
WHITE = "#fbf1e8"  # the cream of the wordmark
MUTED = "#b3a7d6"
SUN = "#fcc774"  # the sun: accent, and the verifier's colour
ROSE = "#c689b2"  # clouds: Codex (val)
ORANGE = "#fd9b72"  # sunset: Claude (spike)
INK = "#131b41"  # text on a filled chip or label
PANEL = "#1b2150"
LINE = "#34306e"
FONTS = Path("/usr/share/fonts/truetype/dejavu")


def font(size: int, bold: bool = False, mono: bool = False) -> Any:
    name = "DejaVuSansMono" if mono else "DejaVuSans"
    return ImageFont.truetype(
        str(FONTS / (name + ("-Bold" if bold else "") + ".ttf")), size
    )


def wrap(draw: Any, text: str, f: Any, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = (line + " " + word).strip()
            if draw.textlength(candidate, font=f) > width and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines


def textblock(
    draw: Any,
    text: str,
    xy: tuple[int, int],
    size: int,
    width: int,
    colour: str = WHITE,
    bold: bool = False,
    mono: bool = False,
) -> int:
    f = font(size, bold, mono)
    lines = wrap(draw, text, f, width)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=f, fill=colour)
        y += int(size * 1.42)
    return y


def backdrop() -> Any:
    im = Image.new("RGB", (W, H), BG)
    # A quiet teal wash, matching the original clip's upper-left light.
    pix = im.load()
    for y in range(H):
        for x in range(W):
            glow = max(0, 1 - math.hypot(x / 1350, y / 1100))
            # Night sky lifting to violet in the upper left, as in the hero.
            pix[x, y] = (19 + int(61 * glow), 27 + int(14 * glow), 65 + int(44 * glow))
    return im


BASE = backdrop()

CHAPTER_NAMES = [
    "A real bug",
    "Dead end",
    "Handoff",
    "Fix",
    "Dispute",
    "Verify",
    "Correction",
    "Validated",
    "Memory only",
    "Human gate",
]
# Chips are coloured by who wrote them; the text says what they are.
AGENT_FILL = {"val": ROSE, "spike": ORANGE, "verifier": SUN}


def rgb(colour: str) -> tuple[int, int, int]:
    return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))


RAIL_SLOTS = 8
# What each chip says: its trust state, then who recorded it and on which model.
CHIP_KIND = {
    "failed": "DEAD END",
    "candidate": "CLAIM",
    "disagreement": "DISPUTE",
    "validated": "VALIDATED",
}
ACTOR = {
    "val": "Codex (val)",
    "spike": "Claude (spike)",
    "verifier": "Codex (verifier)",
    "operator": "Human (jon)",
}
# The human gate is not a memory record: drawn hollow after the rail's chips.
GATE_LINES = ["AWAITING", ACTOR["operator"]]


def chip_fonts() -> tuple[Any, Any]:
    """One font per chip line: what it is, who wrote it."""
    return font(18, True), font(17)


def chapter_box(i: int, count: int = len(CHAPTER_NAMES)) -> Box:
    """Box i of a bar of `count` chapters spanning the frame inside 64 px margins."""
    step = (W - 128 + 16) // count
    x0 = 64 + i * step
    return (x0, 56, x0 + step - 16, 104)


def rail_box(i: int) -> Box:
    x0 = 92 + i * 212
    return (x0, 950, x0 + 196, 1016)


def chip_lines(c: dict[str, Any]) -> list[str]:
    who = str(c["principal"])
    return [CHIP_KIND[c["kind"]], ACTOR.get(who, who)]


def chapter_font(d: Any, names: list[str]) -> Any:
    """The largest label size at which every name fits its box, 14 px either side."""
    x0, _, x1, _ = chapter_box(0, len(names))
    for size in (18, 16, 14):
        f = font(size, True)
        if max(d.textlength(n.upper(), font=f) for n in names) + 28 <= x1 - x0:
            return f
    raise ValueError(f"{len(names)} chapter names do not fit the bar")


def draw_chapters(d: Any, current: int | None, names: list[str]) -> None:
    f = chapter_font(d, names)
    for i, name in enumerate(names):
        x0, y0, x1, y1 = chapter_box(i, len(names))
        active = i == current
        d.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=22,
            fill="#3a2a5e" if active else PANEL,
            outline=SUN if active else LINE,
            width=2,
        )
        d.text((x0 + 14, y0 + 13), name.upper(), font=f, fill=SUN if active else MUTED)


def draw_rail(d: Any, rail: list[dict[str, Any]], gate: bool = False) -> None:
    if len(rail) + gate > RAIL_SLOTS:
        raise ValueError(f"rail has {len(rail) + gate} chips; at most {RAIL_SLOTS} fit")
    slot = {c["fact_id"]: i for i, c in enumerate(rail)}
    # Brackets above the rail, staggered, so a link never runs behind the chips
    # between its ends and reads as linking neighbours.
    links = [(slot[c["linked_from"]], i) for i, c in enumerate(rail)
             if c.get("linked_from") in slot]  # fmt: skip
    for k, (src, dst) in enumerate(links):
        a, b = rail_box(src), rail_box(dst)
        xa, xb = (a[0] + a[2]) // 2, (b[0] + b[2]) // 2
        top = a[1] - 14 - 12 * k
        d.line((xa, a[1], xa, top, xb, top, xb, b[1]), fill=WHITE, width=2)
    for i, c in enumerate(rail):
        x0, y0, x1, y1 = rail_box(i)
        d.rounded_rectangle(
            (x0, y0, x1, y1), radius=14, fill=AGENT_FILL[c["principal"]]
        )
        for line, (text, f) in enumerate(zip(chip_lines(c), chip_fonts(), strict=True)):
            d.text((x0 + 14, y0 + 9 + 25 * line), text, font=f, fill=INK)
    if gate:
        x0, y0, x1, y1 = rail_box(len(rail))
        d.rounded_rectangle((x0, y0, x1, y1), radius=14, outline=WHITE, width=2)
        for line, (text, f) in enumerate(zip(GATE_LINES, chip_fonts(), strict=True)):
            d.text((x0 + 14, y0 + 9 + 25 * line), text, font=f, fill=WHITE)


def render_scene(scene: dict[str, Any], index: int, total: int, footer: str) -> Any:
    im = BASE.copy()
    d = ImageDraw.Draw(im)
    accent = {"Claude": ORANGE, "Codex": ROSE}.get(str(scene.get("speaker")), SUN)
    draw_chapters(d, scene.get("chapter"), scene.get("chapters", CHAPTER_NAMES))
    if scene.get("wall"):
        w = scene["wall"]
        d.text(
            (1828 - d.textlength(w, font=font(22)), 120), w, font=font(22), fill=MUTED
        )
    d.text((92, 157), scene["eyebrow"].upper(), font=font(27, True), fill=accent)
    # One line only: a wrapped title runs behind the panel below it.
    if d.textlength(scene["title"], font=font(56, True)) > 1736:
        raise ValueError(f"Scene {index} title wraps: {scene['title']!r}")
    textblock(d, scene["title"], (92, 214), 56, 1736, bold=True)
    if scene.get("intro"):
        textblock(d, scene["body"], (96, 365), 40, 1590, colour=MUTED)
        for i, (label, desc) in enumerate(
            [
                ("Garden", "Talk together."),
                ("Cairn", "Remember the plan."),
                ("Attic", "Check the source."),
            ]
        ):
            x = 92 + i * 590
            d.rounded_rectangle(
                (x, 575, x + 552, 830),
                radius=27,
                fill=PANEL,
                outline=LINE,
                width=2,
            )
            d.text((x + 34, 613), label, font=font(43, True), fill=SUN)
            d.text((x + 34, 695), desc, font=font(29), fill=WHITE)
    else:
        d.rounded_rectangle(
            (92, 340, 1828, 835), radius=28, fill=PANEL, outline=LINE, width=2
        )
        if scene.get("label"):
            d.rounded_rectangle(
                (
                    130,
                    376,
                    130 + d.textlength(scene["label"], font=font(24, True)) + 42,
                    422,
                ),
                radius=22,
                fill=accent,
            )
            d.text((151, 382), scene["label"], font=font(24, True), fill=INK)
        if scene.get("comparison"):
            d.line((960, 449, 960, 731), fill=LINE, width=2)
            ends = []
            for j, entry in enumerate(scene["comparison"]):
                x = 134 + j * 863
                d.text(
                    (x, 455),
                    entry["label"],
                    font=font(24, True),
                    fill=MUTED if j == 0 else SUN,
                )
                ends.append(
                    textblock(d, entry["body"], (x, 510), scene.get("size", 35), 780)
                )
            end = max(ends)
        else:
            end = textblock(
                d,
                scene["body"],
                (134, 466),
                scene.get("size", 43),
                1645,
                mono=scene.get("mono", False),
            )
        if end > (732 if scene.get("tools") else 791):
            raise ValueError(f"Scene {index} overflows: {end}")
        if scene.get("tools"):
            d.line((134, 752, 1786, 752), fill=LINE, width=2)
            d.text((134, 776), scene["tools"], font=font(23, mono=True), fill=SUN)
        if scene.get("note"):
            textblock(d, scene["note"], (96, 868), 27, 1725, colour=MUTED)
    draw_rail(d, scene.get("rail", []), bool(scene.get("gate")))
    if scene.get("rail_note"):
        note = scene["rail_note"]
        width = d.textlength(note, font=font(21))
        d.text((1660 - width, 1036), note, font=font(21), fill=MUTED)
    d.text((92, 1036), footer, font=font(21), fill=MUTED)
    d.text((1710, 1036), f"{index + 1:02} / {total:02}", font=font(23), fill=MUTED)
    return im


def main() -> None:
    story = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(story.read_text())
    scenes = data["scenes"]
    work = story.parent / "frames"
    work.mkdir(exist_ok=True)
    listing = []
    previous = None
    for i, scene in enumerate(scenes):
        p = work / f"{i:02}.png"
        im = render_scene(scene, i, len(scenes), data["footer"])
        im.save(p)
        if i == 0:
            im.save(out.with_suffix(".png"))
        if previous is not None:
            for step in range(1, 7):
                transition = work / f"{i:02}-fade-{step}.png"
                Image.blend(previous, im, step / 6).save(transition)
                listing.extend(
                    [f"file '{transition.resolve()}'", "duration 0.0416666667"]
                )
        listing.extend(
            [
                f"file '{p.resolve()}'",
                f"duration {scene['duration'] - (0.25 if previous is not None else 0)}",
            ]
        )
        previous = im
    listing.append(f"file '{p.resolve()}'")
    concat = work / "frames.txt"
    concat.write_text("\n".join(listing) + "\n")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vf",
            "fps=24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out.with_suffix(".mp4")),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out.with_suffix(".mp4")),
            "-vf",
            "fps=8,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3",
            "-loop",
            "0",
            str(out.with_suffix(".gif")),
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "duration": sum(s["duration"] for s in scenes),
                "scenes": len(scenes),
                "output": str(out),
            }
        )
    )


if __name__ == "__main__":
    main()
