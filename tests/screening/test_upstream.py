import inspect
import json
from pathlib import Path

import pytest
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HexHighEntropyString,
)
from detect_secrets.plugins.keyword import KeywordDetector
from detect_secrets.settings import get_settings
from detect_secrets.util.filetype import FileType, determine_file_type

from cairn.screening import POLICY_VERSION, SecretScreen, upstream
from cairn.screening.upstream import (
    BASE64_ENTROPY_LIMIT,
    DETECTOR_RULES,
    EXCLUDED_DETECTOR,
    HEX_ENTROPY_LIMIT,
    KEYWORD_EXCLUDE,
    PINNED_DETECTORS,
    installed_detectors,
)

_CORPUS_PATH = Path(__file__).parent / "secret-corpus.json"
_FIELD_PATH = "facts.0.body"


def test_the_pinned_set_is_the_installed_inventory_minus_the_one_exclusion() -> None:
    # I-75's parity requirement. This fails on an upstream addition as well as
    # a removal, so a new detector cannot join the policy without the identity
    # being pinned, the corpus exercising it and this decision being revisited.
    assert installed_detectors() - {EXCLUDED_DETECTOR} == set(PINNED_DETECTORS)


def test_the_pinned_set_has_twenty_six_identities_and_no_duplicates() -> None:
    assert len(PINNED_DETECTORS) == 26
    assert len(set(PINNED_DETECTORS)) == 26


def test_the_excluded_detector_is_installed_rather_than_absent() -> None:
    # The exclusion must be a decision, not an accident of what shipped: if the
    # distribution dropped IPPublicDetector the parity test above would still
    # pass, and this is what notices.
    assert EXCLUDED_DETECTOR in installed_detectors()


def test_the_pinned_settings_match_the_distribution_defaults() -> None:
    # P-25: the defaults are restated as literals so an upstream change to any
    # of them is a visible diff rather than a silent change of policy. This is
    # the check that makes that claim true.
    defaults = {
        Base64HighEntropyString: ("limit", BASE64_ENTROPY_LIMIT),
        HexHighEntropyString: ("limit", HEX_ENTROPY_LIMIT),
        KeywordDetector: ("keyword_exclude", KEYWORD_EXCLUDE),
    }
    for plugin, (parameter, pinned) in defaults.items():
        actual = inspect.signature(plugin).parameters[parameter].default
        assert actual == pinned, (plugin.__name__, parameter, actual, pinned)


def test_every_pinned_detector_has_a_rule_and_the_identities_agree() -> None:
    assert [suffix for suffix, _ in DETECTOR_RULES] == [
        f"upstream/{name}" for name in PINNED_DETECTORS
    ]


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\x0b", id="vertical-tab"),
        pytest.param("\x0c", id="form-feed"),
        pytest.param("\x1c", id="file-separator"),
        pytest.param("\x1d", id="group-separator"),
        pytest.param("\x1e", id="record-separator"),
        pytest.param("\x85", id="next-line"),
        pytest.param(" ", id="line-separator"),
        pytest.param(" ", id="paragraph-separator"),
    ],
)
def test_an_invisible_separator_does_not_smuggle_a_secret_past_a_detector(
    separator: str,
) -> None:
    # str.splitlines() breaks on every one of these, but regex ``\s`` matches
    # them too, so a detector tolerating whitespace between a keyword and its
    # value would bridge the character while a splitlines() scan had already
    # cut the line in two. One invisible byte was enough to smuggle a password
    # past KeywordDetector; _matcher splits on real newlines only.
    content = f'password ={separator} "aB3xY9kQ7mZ2pL5vN8wR"'
    rules = {finding.rule for finding in SecretScreen().screen(_FIELD_PATH, content)}
    assert f"{POLICY_VERSION}/upstream/KeywordDetector" in rules


def test_the_constant_filename_resolves_to_the_default_regex_family() -> None:
    # KeywordDetector passes the filename to determine_file_type and picks a
    # different regex family per extension, so _FILENAME is not inert. Giving
    # it a dot would silently change which secrets are found, with no other
    # code touched.
    assert determine_file_type(upstream._FILENAME) is FileType.OTHER


def test_the_network_verification_path_is_replaced_on_every_plugin() -> None:
    # Structural, not contingent: analyze_line reaches verify through
    # detect_secrets' process-wide settings, which Cairn does not own.
    for _, plugin in upstream._PLUGINS:
        with pytest.raises(RuntimeError, match="network path"):
            plugin.verify("secret")


def test_the_settings_filter_that_would_trigger_verification_is_absent() -> None:
    # The condition under which analyze_line would call verify. Cairn never
    # configures these settings and the key is not a distribution default, but
    # they are global mutable state, so this fails loudly if that ever changes.
    trigger = "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
    assert trigger not in get_settings().filters


def test_a_low_entropy_quoted_string_is_not_reported_as_a_secret() -> None:
    # The reason upstream.py uses analyze_line rather than analyze_string.
    # HighEntropyStringsPlugin.analyze_string deliberately skips the Shannon
    # check, so scanning strings would report every long quoted value as a
    # secret and discard the pinned limits entirely. This pins the consequence:
    # a zero-entropy quoted value is not a finding.
    findings = SecretScreen().screen(_FIELD_PATH, 'note = "aaaaaaaaaaaaaaaaaaaaaaaa"')
    assert findings == ()


def test_a_high_entropy_quoted_string_is_reported() -> None:
    content = 'secret = "aB3xY9kQ7mZ2pL5vN8wR4tG6sD1fH0jCeI7uO2yP6bV"'
    rules = {finding.rule for finding in SecretScreen().screen(_FIELD_PATH, content)}
    assert f"{POLICY_VERSION}/upstream/Base64HighEntropyString" in rules


def test_the_openai_detector_covers_the_sk_proj_shape() -> None:
    # Carried from Task 1: the Cairn-owned generic sk- pattern structurally
    # cannot match sk-proj- keys, because it needs an unbroken alphanumeric run
    # and proj- breaks it after four characters. The upstream detector keys off
    # the T3BlbkFJ marker instead, so the shape is covered after all.
    token = "sk-proj-" + "a" * 20 + "T3BlbkFJ" + "b" * 20
    rules = {finding.rule for finding in SecretScreen().screen(_FIELD_PATH, token)}
    assert f"{POLICY_VERSION}/upstream/OpenAIDetector" in rules


def test_upstream_detection_never_calls_the_network_verification_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # I-31 requires screening to complete before any remote dependency call and
    # P-24 requires purity, so verify() - the library's only network path -
    # must never run. Each plugin's verify is replaced with a detonator; a
    # monkeypatch, so the substitution cannot leak into another test.
    #
    # Only the upstream positives are replayed, and every detector is required
    # to fire. A detector that never fired could not call verify either, so
    # without that second assertion this test would pass vacuously.
    calls: list[str] = []
    for name, plugin in upstream._PLUGINS:

        def detonate(*args: object, _name: str = name, **kwargs: object) -> None:
            calls.append(_name)

        monkeypatch.setattr(plugin, "verify", detonate)

    corpus = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    screen = SecretScreen()
    fired: set[str] = set()
    for entry in corpus["entries"]:
        rule = str(entry["rule"])
        if not rule.startswith(f"{POLICY_VERSION}/upstream/"):
            continue
        if str(entry["verdict"]) != "positive":
            continue
        found = {
            finding.rule
            for finding in screen.screen(_FIELD_PATH, str(entry["content"]))
        }
        fired |= found & {f"{POLICY_VERSION}/{suffix}" for suffix, _ in DETECTOR_RULES}
    assert calls == []
    assert fired == {f"{POLICY_VERSION}/{suffix}" for suffix, _ in DETECTOR_RULES}
