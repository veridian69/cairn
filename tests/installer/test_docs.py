from pathlib import Path

from cairn_install.workflow import STAGES

ROOT = Path(__file__).parents[2]
GUIDE = ROOT / "docs" / "operations" / "guided-installation.md"


def test_guided_installation_stage_headings_match_workflow() -> None:
    text = GUIDE.read_text()
    headings = [
        line.removeprefix("### ")
        for line in text.splitlines()
        if line.startswith("### ") and " — " in line
    ]

    assert headings == [f"{key} — {title}" for key, title in STAGES]


def test_install_indexes_link_to_guided_installation() -> None:
    target = "guided-installation.md"
    assert target in (ROOT / "docs" / "install.md").read_text()
    assert "docs/operations/guided-installation.md" in (ROOT / "README.md").read_text()
