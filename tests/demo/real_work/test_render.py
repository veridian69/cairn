import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytest.importorskip("PIL")

from scripts.real_work_demo import render  # noqa: E402

SUN = (0xFC, 0xC7, 0x74)  # the hero's sun: accent and the verifier's colour


def scene(rail: list[dict[str, Any]]) -> dict[str, Any]:
    return {"duration": 3, "chapter": 5, "eyebrow": "05 / Verify", "title": "Verifier (Codex)",
            "label": "EXACT EXCERPT", "body": "1271 passed", "rail": rail, "wall": "T4: 4 m 10 s"}  # fmt: skip


def chip(fact_id: str, kind: str, who: str, link: str | None = None) -> dict[str, Any]:
    return {"fact_id": fact_id, "kind": kind, "principal": who, "linked_from": link}


def test_chapter_bar_highlights_current_chapter() -> None:
    img = render.render_scene(scene([]), 0, 1, "")
    assert img.size == (1920, 1080)
    assert render.chapter_box(5)[0] > render.chapter_box(4)[0]
    x0, y0, x1, _ = render.chapter_box(5)
    assert SUN in {img.getpixel((x, y0)) for x in range(x0, x1)}
    x0, y0, x1, _ = render.chapter_box(4)
    assert SUN not in {img.getpixel((x, y0)) for x in range(x0, x1)}


def test_validated_chip_is_drawn_in_mint_on_the_rail() -> None:
    img = render.render_scene(
        scene(
            [
                chip("c1", "candidate", "spike"),
                chip("v1", "validated", "verifier", "c1"),
            ]
        ),
        0,
        1,
        "",
    )
    x0, y0, x1, y1 = render.rail_box(1)
    assert SUN in {img.getpixel((x, (y0 + y1) // 2)) for x in range(x0, x1)}
    x0, y0, x1, y1 = render.rail_box(0)
    assert SUN not in {
        img.getpixel((x, (y0 + y1) // 2)) for x in range(x0 + 10, x1 - 10)
    }


def test_rail_overflow_is_refused_not_clipped() -> None:
    with pytest.raises(ValueError, match="rail"):
        render.render_scene(
            scene([chip(f"c{i}", "candidate", "spike") for i in range(9)]), 0, 1, ""
        )


def test_a_curated_rail_caption_is_drawn_beside_the_footer() -> None:
    plain = render.render_scene(scene([]), 0, 1, "")
    noted = render.render_scene(
        {**scene([]), "rail_note": "8 of 13 memory records shown"}, 0, 1, ""
    )
    box = (900, 1030, 1680, 1070)
    assert plain.crop(box).tobytes() != noted.crop(box).tobytes()


def test_the_chapter_bar_sizes_itself_to_the_chapter_count() -> None:
    ten = render.CHAPTER_NAMES
    assert len(ten) == 10
    assert render.chapter_box(9, 10)[2] <= 1920 - 64
    assert render.chapter_box(7, 8)[0] > render.chapter_box(7, 10)[0]
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    x0, _, x1, _ = render.chapter_box(0, 10)
    f = render.chapter_font(d, list(ten))
    assert max(d.textlength(n.upper(), font=f) for n in ten) + 28 <= x1 - x0
    # Eight chapters keep the original 18 px labels.
    assert render.chapter_font(d, list(ten[:6]) + list(ten[8:])).size == 18


def test_a_scene_names_its_own_chapters() -> None:
    eight = [
        "A real bug",
        "Dead end",
        "Handoff",
        "Fix",
        "Dispute",
        "Verify",
        "Memory only",
        "Human gate",
    ]
    img = render.render_scene({**scene([]), "chapters": eight, "chapter": 7}, 0, 1, "")
    x0, y0, x1, _ = render.chapter_box(7, 8)
    assert SUN in {img.getpixel((x, y0)) for x in range(x0, x1)}


def test_links_between_distant_chips_arc_above_the_rail_not_through_it() -> None:
    img = render.render_scene(
        scene([chip("a", "failed", "val"), chip("b", "candidate", "spike"),
               chip("c", "candidate", "val"), chip("d", "disagreement", "spike", "a")]),
        0, 1, "",
    )  # fmt: skip
    white = (0xFB, 0xF1, 0xE8)
    _, y0, x1, y1 = render.rail_box(1)
    gap = [
        img.getpixel((x, (y0 + y1) // 2)) for x in range(x1 + 1, render.rail_box(2)[0])
    ]
    assert white not in gap  # nothing drawn between b and c: they are not linked
    ax = (render.rail_box(0)[0] + render.rail_box(0)[2]) // 2
    above = [img.getpixel((ax, y)) for y in range(y0 - 40, y0)]
    assert white in above


def test_a_chip_says_plainly_what_it_is_and_who_wrote_it_without_ids() -> None:
    """Operator, 25 September 2026: a visual tag line, readable at a glance."""
    assert render.chip_lines(chip("a1037797-0e49", "validated", "verifier", "c1")) == [
        "VALIDATED", "Codex (verifier)",
    ]  # fmt: skip
    assert render.chip_lines(chip("875ed73e-6665", "candidate", "spike")) == [
        "CLAIM", "Claude (spike)",
    ]  # fmt: skip
    assert render.chip_lines(chip("dcd421c4-7fe0", "failed", "val"))[0] == "DEAD END"
    assert (
        render.chip_lines(chip("e1aeb91e-1dd7", "disagreement", "spike"))[0]
        == "DISPUTE"
    )
    assert render.GATE_LINES == ["AWAITING", "Human (jon)"]


def test_every_chip_label_fits_its_chip() -> None:
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    x0, _, x1, _ = render.rail_box(0)
    for kind in render.CHIP_KIND:
        for who in ("val", "spike", "verifier"):
            for line, f in zip(
                render.chip_lines(chip("0" * 36, kind, who)),
                render.chip_fonts(),
                strict=True,
            ):
                assert d.textlength(line, font=f) + 28 <= x1 - x0, (kind, who, line)


def test_the_human_gate_is_an_outlined_chip_not_a_memory_record() -> None:
    assert render.ACTOR["operator"] == "Human (jon)"
    rail = [chip("v1", "validated", "verifier")]
    img = render.render_scene({**scene(rail), "gate": True}, 0, 1, "")
    x0, y0, x1, y1 = render.rail_box(1)
    inside = img.getpixel(((x0 + x1) // 2, y1 - 6))
    assert inside not in {
        render.rgb(c) for c in render.AGENT_FILL.values()
    }  # hollow, unlike a record
    edge = {img.getpixel((x, y0)) for x in range(x0 + 20, x1 - 20)}
    assert (0xFB, 0xF1, 0xE8) in edge
    with pytest.raises(ValueError, match="rail"):
        render.render_scene(
            {**scene([chip(f"c{i}", "candidate", "spike") for i in range(8)]), "gate": True},
            0, 1, "",
        )  # fmt: skip


def test_a_title_must_fit_one_line_or_the_render_refuses() -> None:
    fits = "The verifier re-runs everything and settles the dispute"
    render.render_scene({**scene([]), "title": fits}, 0, 1, "")
    with pytest.raises(ValueError, match="title"):
        render.render_scene(
            {**scene([]), "title": fits + " and then some more"}, 0, 1, ""
        )


def test_each_agent_keeps_one_colour_from_the_hero_whatever_it_records() -> None:
    """Operator, 25 September 2026: colours from the Cairn hero, consistent per agent."""
    assert render.AGENT_FILL == {
        "val": "#c689b2",
        "spike": "#fd9b72",
        "verifier": "#fcc774",
    }
    rail = [chip("f", "failed", "val"), chip("c", "candidate", "val"),
            chip("s", "candidate", "spike"), chip("v", "validated", "verifier")]  # fmt: skip
    img = render.render_scene(scene(rail), 0, 1, "")

    def fill(i: int) -> tuple[int, int, int]:
        x0, y0, _, y1 = render.rail_box(i)
        return tuple(img.getpixel((x0 + 4, (y0 + y1) // 2)))

    assert fill(0) == fill(1) == render.rgb("#c689b2")
    assert fill(2) == render.rgb("#fd9b72") and fill(3) == render.rgb("#fcc774")
