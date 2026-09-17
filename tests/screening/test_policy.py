import ast
import json
import re
import sys
import unicodedata
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from uuid import UUID

import pytest

from cairn.authority.credentials import mint_token
from cairn.screening import (
    ALL_RULES,
    POLICY_VERSION,
    SecretFinding,
    SecretScreen,
    audit_reason_code,
    first_finding,
    policy,
)
from cairn.screening.policy import CAIRN_RULES, UPSTREAM_RULES

_CORPUS_PATH = Path(__file__).parent / "secret-corpus.json"
# The bracketed form is the seam's convention (Task 3) and now also the
# wire layer's (`errors._field_path`); trued up here per the Task 3 ledger
# note deferring exactly this to Task 4.
_FIELD_PATH = "facts[0].body"
_DOCUMENT = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def _corpus_entries() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "rule": str(entry["rule"]),
            "verdict": str(entry["verdict"]),
            "content": str(entry["content"]),
        }
        for entry in _DOCUMENT["entries"]
    )


_ENTRIES = _corpus_entries()


def _rule_identities(findings: tuple[SecretFinding, ...]) -> set[str]:
    return {finding.rule for finding in findings}


@pytest.mark.parametrize(
    ("rule", "verdict", "content"),
    [
        pytest.param(
            entry["rule"],
            entry["verdict"],
            entry["content"],
            id=f"{entry['rule'].rsplit('/', 1)[-1]}-{entry['verdict']}-{index}",
        )
        for index, entry in enumerate(_ENTRIES)
    ],
)
def test_every_corpus_entry_matches_its_recorded_verdict(
    rule: str, verdict: str, content: str
) -> None:
    found = _rule_identities(SecretScreen().screen(_FIELD_PATH, content))
    if verdict == "positive":
        assert rule in found
    else:
        assert rule not in found


def test_the_corpus_exercises_every_rule_identity() -> None:
    # I-75: CI fails if any rule identity is unexercised. This covers the six
    # Cairn-owned rules and all twenty-six pinned upstream identities, and
    # fails in both directions - an unexercised rule and a corpus entry naming
    # a rule the policy does not have.
    assert {entry["rule"] for entry in _ENTRIES} == ALL_RULES


def test_every_rule_has_both_a_positive_and_a_negative_entry() -> None:
    for rule in ALL_RULES:
        verdicts = {e["verdict"] for e in _ENTRIES if e["rule"] == rule}
        assert verdicts == {"positive", "negative"}, rule


def test_the_policy_version_is_the_frozen_identity() -> None:
    assert POLICY_VERSION == "cairn.secret/v1"


def test_the_corpus_declares_the_policy_it_specifies() -> None:
    assert _DOCUMENT["policy"] == POLICY_VERSION


def test_screening_empty_text_finds_nothing() -> None:
    assert SecretScreen().screen(_FIELD_PATH, "") == ()


def test_screening_ordinary_prose_finds_nothing() -> None:
    prose = "The reconciliation loop retried twice and then settled on Friday."
    assert SecretScreen().screen(_FIELD_PATH, prose) == ()


def test_a_finding_carries_a_rule_identity_and_a_field_path_and_nothing_else() -> None:
    # I-31: a finding may not disclose the matched text, its offsets or its
    # surrounding context, so it cannot leak what it found even if mishandled.
    assert tuple(field.name for field in fields(SecretFinding)) == (
        "rule",
        "field_path",
    )


def test_a_finding_is_frozen() -> None:
    finding = SecretFinding(rule=f"{POLICY_VERSION}/pem-block", field_path=_FIELD_PATH)
    with pytest.raises(FrozenInstanceError):
        finding.rule = "other"  # type: ignore[misc]


def test_a_finding_echoes_the_field_path_it_was_given() -> None:
    content = "-----BEGIN PRIVATE KEY-----"
    findings = SecretScreen().screen("metadata.canonical", content)
    assert findings != ()
    assert {finding.field_path for finding in findings} == {"metadata.canonical"}


def test_no_finding_repr_discloses_the_content_that_produced_it() -> None:
    for entry in _ENTRIES:
        if entry["verdict"] != "positive":
            continue
        findings = SecretScreen().screen(_FIELD_PATH, entry["content"])
        rendered = repr(findings)
        for token in entry["content"].split():
            if len(token) >= 16:
                assert token not in rendered


def test_screen_returns_only_identities_from_the_closed_vocabulary() -> None:
    for entry in _ENTRIES:
        findings = SecretScreen().screen(_FIELD_PATH, entry["content"])
        assert _rule_identities(findings) <= ALL_RULES


def test_findings_are_sorted_by_field_path_then_rule() -> None:
    content = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Authorization: Bearer c3ludGhldGljLWJlYXJlci12YWx1ZS0wMDAwMDA\n"
        "postgres://cairn_app:synthetic-passphrase@db.example.invalid/cairn\n"
    )
    findings = SecretScreen().screen(_FIELD_PATH, content)
    assert len(findings) >= 3
    keys = [(finding.field_path, finding.rule) for finding in findings]
    assert keys == sorted(keys)


def test_a_rule_matching_repeatedly_yields_exactly_one_finding() -> None:
    # SecretFinding carries no offset, so two matches of one rule would be two
    # identical values. Collapsing them is forced by the value's shape. The
    # upstream PrivateKeyDetector fires on this content too, which is why the
    # assertion counts one rule rather than the whole result.
    once = "-----BEGIN RSA PRIVATE KEY-----"
    findings = SecretScreen().screen(_FIELD_PATH, f"{once}\n{once}\n{once}")
    rules = [finding.rule for finding in findings]
    assert rules.count(f"{POLICY_VERSION}/pem-block") == 1
    assert len(rules) == len(set(rules))


def test_the_cairn_owned_and_upstream_vocabularies_partition_the_policy() -> None:
    assert CAIRN_RULES | UPSTREAM_RULES == ALL_RULES
    assert CAIRN_RULES & UPSTREAM_RULES == frozenset()
    assert len(CAIRN_RULES) == 6
    assert len(UPSTREAM_RULES) == 26


def test_the_corpus_keeps_a_positive_that_only_the_hex_floor_catches() -> None:
    # The base64 floor is 3.5 and the hex floor 2.5. Every other hex value in
    # the corpus sits near 3.95 entropy and trips the base64 floor first, so
    # without a case between the two floors the hex floor is unreachable and
    # the ruling that set it is dead policy. The parametrised corpus test
    # cannot notice this entry being deleted - it would simply run one case
    # fewer - so the corpus is pinned here rather than the behaviour, which
    # that test already covers.
    positives = {
        entry["content"] for entry in _ENTRIES if entry["verdict"] == "positive"
    }
    assert "api_key: 0011223344556677" in positives


def test_a_candidate_beyond_the_keyword_window_does_not_fire() -> None:
    rule = f"{POLICY_VERSION}/contextual-entropy"
    secret = "Zm9vYmFyc3ludGhldGljcGFzc3dvcmR2YWx1ZQ"
    near = f"password {'x' * 8} {secret}"
    far = f"password {'x' * 200} {secret}"
    assert rule in _rule_identities(SecretScreen().screen(_FIELD_PATH, near))
    assert rule not in _rule_identities(SecretScreen().screen(_FIELD_PATH, far))


def test_the_authorization_header_floor_is_exact_on_both_sides() -> None:
    rule = f"{POLICY_VERSION}/authorization-header"
    fifteen = SecretScreen().screen(_FIELD_PATH, f"Authorization: Bearer {'a1' * 7}b")
    sixteen = SecretScreen().screen(_FIELD_PATH, f"Authorization: Bearer {'a1' * 8}")
    assert rule not in _rule_identities(fifteen)
    assert rule in _rule_identities(sixteen)


def test_a_digit_free_authorization_value_is_not_credential_shaped() -> None:
    # The value charset admits ``-``, ``_`` and ``.``, so without the digit
    # requirement any sixteen-character hyphenated placeholder or reason code
    # after "authorization:" was permanently rejected — facts *about*
    # authorisation behaviour became unwritable. A real credential without a
    # digit is vanishingly rare, and contextual entropy (``bearer`` is a
    # keyword) remains in front of the residual.
    rule = f"{POLICY_VERSION}/authorization-header"
    screen = SecretScreen()
    placeholder = "set Authorization: Bearer your-token-goes-here"
    reason_code = "authorization: revocation_not_authorised"
    assert rule not in _rule_identities(screen.screen(_FIELD_PATH, placeholder))
    assert rule not in _rule_identities(screen.screen(_FIELD_PATH, reason_code))


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        pytest.param(
            "-----BE​GIN RSA PRIVATE KEY-----",
            f"{POLICY_VERSION}/pem-block",
            id="zero-width-space-inside-a-literal",
        ),
        pytest.param(
            "AK­IA0123456789ABCDEF",
            f"{POLICY_VERSION}/upstream/AWSKeyDetector",
            id="soft-hyphen-inside-a-token",
        ),
        pytest.param(
            "ＡＫＩＡ" + "0123456789ABCDEF",
            f"{POLICY_VERSION}/upstream/AWSKeyDetector",
            id="fullwidth-transliteration",
        ),
    ],
)
def test_an_obfuscated_secret_is_folded_before_screening(
    content: str, rule: str
) -> None:
    # Each of these evaded the whole policy before the fold: every rule is an
    # ASCII-literal pattern, so one invisible character or one fullwidth letter
    # was enough. The accidental case - a key pasted out of a web page or a PDF
    # with formatting characters still in it - is the one this buys down.
    assert rule in _rule_identities(SecretScreen().screen(_FIELD_PATH, content))


def test_the_declined_obfuscations_are_recorded_as_still_evading() -> None:
    # Stated in the module docstring rather than fixed, so the claim is pinned
    # here too: if either ever starts being caught, the docstring is wrong and
    # this test says so. A homoglyph survives NFKC, and a format character
    # substituted for a required space leaves its neighbours joined.
    screen = SecretScreen()
    homoglyph = "АKIA0123456789ABCDEF"
    substituted = "-----BEGIN RSA PRIVATE​KEY-----"
    assert _rule_identities(screen.screen(_FIELD_PATH, homoglyph)) == set()
    assert f"{POLICY_VERSION}/pem-block" not in _rule_identities(
        screen.screen(_FIELD_PATH, substituted)
    )


def test_ascii_text_passes_through_the_fold_unchanged() -> None:
    # The fast path is provably the identity, not merely an optimisation: ASCII
    # is NFKC-normal and the lowest format character is U+00AD.
    for entry in _ENTRIES:
        if entry["content"].isascii():
            assert policy.normalise_for_screening(entry["content"]) is entry["content"]


def test_the_pinned_format_characters_match_the_running_unicode_database() -> None:
    # The same shape as P-25's detector parity check: the constant is pinned so
    # nothing is swept at import, and this fails if a Python upgrade adds a
    # format character that would otherwise leave a hole.
    live = {
        codepoint
        for codepoint in range(sys.maxunicode + 1)
        if unicodedata.category(chr(codepoint)) == "Cf"
    }
    assert set(policy._FORMAT_CHARACTERS) == live


def test_the_fold_is_idempotent_across_an_exposed_composition_boundary() -> None:
    # The closure-review finding over c9ccda2: U+200B has combining class 0,
    # so it blocks canonical composition — a fold that normalises before
    # stripping leaves A + U+030A exposed, and a second application composes
    # them to Å. The fold must be its own fixed point or the custody seam's
    # pre-folded copy is screened over a rendering the policy never judged.
    text = "client_secretA\u200b\u030a: 0011223344556677"
    once = policy.normalise_for_screening(text)
    assert policy.normalise_for_screening(once) == once


def test_screening_a_pre_folded_copy_changes_no_verdict() -> None:
    # The invariant the custody seam's masked() relies on (I-96): it hands
    # ``screen`` a pre-folded copy, so folding must move no finding.
    screen = SecretScreen()
    text = "client_secretA\u200b\u030a: 0011223344556677"
    folded = policy.normalise_for_screening(text)
    assert screen.screen(_FIELD_PATH, folded) == screen.screen(_FIELD_PATH, text)


def test_nfkc_reintroduces_no_format_character() -> None:
    # The idempotence proof's premise: stripping first is stable only because
    # NFKC never emits a format character. Pinned like the Cf parity check
    # above so a Unicode database upgrade cannot silently break it.
    for codepoint in range(sys.maxunicode + 1):
        character = chr(codepoint)
        if unicodedata.category(character) in {"Cf", "Cs"}:
            continue
        normalised = unicodedata.normalize("NFKC", character)
        assert not any(unicodedata.category(folded) == "Cf" for folded in normalised), (
            hex(codepoint)
        )


def test_screen_is_deterministic_across_repeated_calls() -> None:
    screen = SecretScreen()
    for entry in _ENTRIES:
        first = screen.screen(_FIELD_PATH, entry["content"])
        second = screen.screen(_FIELD_PATH, entry["content"])
        assert first == second


def test_screen_keeps_no_state_between_interleaved_calls() -> None:
    # P-24: no state between calls. A screen that accumulated anything would
    # give a different answer for the same input depending on what preceded it.
    screen = SecretScreen()
    clean = "The reconciliation loop retried twice."
    dirty = "-----BEGIN RSA PRIVATE KEY-----"
    baseline_clean = screen.screen(_FIELD_PATH, clean)
    baseline_dirty = screen.screen(_FIELD_PATH, dirty)
    for _ in range(3):
        assert screen.screen(_FIELD_PATH, dirty) == baseline_dirty
        assert screen.screen(_FIELD_PATH, clean) == baseline_clean


def test_the_screen_catches_a_token_minted_by_the_real_credential_path() -> None:
    # policy.py cannot import cairn.authority.credentials - that module reaches
    # into the catalogue and this screen is pure - so I-62's pattern is
    # necessarily written out twice. This is what stops the two copies drifting
    # apart: a token minted by the real path must still be caught here, whatever
    # either pattern says.
    minted = mint_token(
        UUID("11111111-1111-4111-8111-111111111111"),
        lambda size: bytes(range(size)),
    )
    findings = SecretScreen().screen(_FIELD_PATH, f"the worker used {minted.text}")
    assert f"{POLICY_VERSION}/cairn-token" in _rule_identities(findings)


def test_the_screening_package_imports_nothing_that_could_make_it_impure() -> None:
    # Purity is expressed structurally in this codebase rather than declared,
    # as in cairn.authority.grants. Reading the import graph pins the absence a
    # reader would otherwise take on trust, and pins the plan's "no transport
    # or catalogue imports" constraint in the same assertion. P-23's module
    # split is pinned too: only upstream.py may reach for detect_secrets.
    allowed = {
        "bisect",
        "cairn",
        "collections",
        "dataclasses",
        "detect_secrets",
        "math",
        "re",
        "typing",
        "unicodedata",
    }
    package = Path(__file__).parents[2] / "src" / "cairn" / "screening"
    modules = sorted(package.glob("*.py"))
    assert [module.name for module in modules] == [
        "__init__.py",
        "policy.py",
        "upstream.py",
    ]
    for module in modules:
        roots: set[str] = set()
        internal: set[str] = set()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
                    if alias.name.startswith("cairn"):
                        internal.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                # "from . import transport" carries no module name, so guarding
                # on node.module would let a relative import past unseen.
                name = node.module or "."
                roots.add(name.split(".")[0])
                if name == "." or name.startswith("cairn"):
                    internal.add(name)
        assert roots <= allowed, (module.name, roots - allowed)
        for name in internal:
            assert name.startswith("cairn.screening."), (module.name, name)
        if module.name != "upstream.py":
            assert "detect_secrets" not in roots, module.name


# === The audit reason codes (P-26) ===========================================

# Written out rather than derived. ``audit_reason_code`` computes the stem so
# that it cannot fall out of step with the rule vocabulary, which leaves this
# literal as the only thing pinning what the derivation actually produces: an
# expectation built by calling the function would agree with any rewrite of
# it, including a wrong one. Changing a value here changes what an operator's
# audit query matches, so it is a policy change and should read as one.
_AUDIT_REASON_CODES = {
    "cairn.secret/v1/authorization-header": "secret_authorization_header",
    "cairn.secret/v1/cairn-token": "secret_cairn_token",
    "cairn.secret/v1/contextual-entropy": "secret_contextual_entropy",
    "cairn.secret/v1/credential-uri": "secret_credential_uri",
    "cairn.secret/v1/pem-block": "secret_pem_block",
    "cairn.secret/v1/provider-token": "secret_provider_token",
    "cairn.secret/v1/upstream/AWSKeyDetector": "secret_upstream_awskeydetector",
    "cairn.secret/v1/upstream/ArtifactoryDetector": (
        "secret_upstream_artifactorydetector"
    ),
    "cairn.secret/v1/upstream/AzureStorageKeyDetector": (
        "secret_upstream_azurestoragekeydetector"
    ),
    "cairn.secret/v1/upstream/Base64HighEntropyString": (
        "secret_upstream_base64highentropystring"
    ),
    "cairn.secret/v1/upstream/BasicAuthDetector": "secret_upstream_basicauthdetector",
    "cairn.secret/v1/upstream/CloudantDetector": "secret_upstream_cloudantdetector",
    "cairn.secret/v1/upstream/DiscordBotTokenDetector": (
        "secret_upstream_discordbottokendetector"
    ),
    "cairn.secret/v1/upstream/GitHubTokenDetector": (
        "secret_upstream_githubtokendetector"
    ),
    "cairn.secret/v1/upstream/GitLabTokenDetector": (
        "secret_upstream_gitlabtokendetector"
    ),
    "cairn.secret/v1/upstream/HexHighEntropyString": (
        "secret_upstream_hexhighentropystring"
    ),
    "cairn.secret/v1/upstream/IbmCloudIamDetector": (
        "secret_upstream_ibmcloudiamdetector"
    ),
    "cairn.secret/v1/upstream/IbmCosHmacDetector": "secret_upstream_ibmcoshmacdetector",
    "cairn.secret/v1/upstream/JwtTokenDetector": "secret_upstream_jwttokendetector",
    "cairn.secret/v1/upstream/KeywordDetector": "secret_upstream_keyworddetector",
    "cairn.secret/v1/upstream/MailchimpDetector": "secret_upstream_mailchimpdetector",
    "cairn.secret/v1/upstream/NpmDetector": "secret_upstream_npmdetector",
    "cairn.secret/v1/upstream/OpenAIDetector": "secret_upstream_openaidetector",
    "cairn.secret/v1/upstream/PrivateKeyDetector": "secret_upstream_privatekeydetector",
    "cairn.secret/v1/upstream/PypiTokenDetector": "secret_upstream_pypitokendetector",
    "cairn.secret/v1/upstream/SendGridDetector": "secret_upstream_sendgriddetector",
    "cairn.secret/v1/upstream/SlackDetector": "secret_upstream_slackdetector",
    "cairn.secret/v1/upstream/SoftlayerDetector": "secret_upstream_softlayerdetector",
    "cairn.secret/v1/upstream/SquareOAuthDetector": (
        "secret_upstream_squareoauthdetector"
    ),
    "cairn.secret/v1/upstream/StripeDetector": "secret_upstream_stripedetector",
    "cairn.secret/v1/upstream/TelegramBotTokenDetector": (
        "secret_upstream_telegrambottokendetector"
    ),
    "cairn.secret/v1/upstream/TwilioKeyDetector": "secret_upstream_twiliokeydetector",
}


def test_the_audit_reason_codes_are_the_frozen_enumeration() -> None:
    assert {rule: audit_reason_code(rule) for rule in ALL_RULES} == (
        _AUDIT_REASON_CODES
    )


def test_every_rule_has_a_distinct_audit_reason_code() -> None:
    """A collision would make a denial event ambiguous about which rule fired,
    which is the one thing P-26 asks the reason code to carry."""
    assert len(set(_AUDIT_REASON_CODES.values())) == len(ALL_RULES)


@pytest.mark.parametrize("reason_code", sorted(_AUDIT_REASON_CODES.values()))
def test_every_audit_reason_code_satisfies_the_audit_grammar(reason_code: str) -> None:
    """``cairn.audit/v1`` rejects a malformed reason code with
    ``invalid_reason_code``, and it would do so *while appending the denial* —
    turning a clean refusal into an internal error. This is a duplicate of
    ``audit._REASON_CODE`` on purpose: the two modules must agree, and the
    screening side must fail here rather than in production."""
    assert re.fullmatch(r"[a-z](?:[a-z0-9_]{0,61}[a-z0-9])?", reason_code)


# === first_finding, the seam's field walk ====================================


def test_first_finding_returns_none_when_every_field_is_clean() -> None:
    screen = SecretScreen()

    assert (
        first_finding(screen, (("a", "the build is green"), ("b", "attempt 2"))) is None
    )


def test_first_finding_stops_at_the_first_dirty_field() -> None:
    screen = SecretScreen()
    pem = "-----BEGIN RSA PRIVATE KEY-----"

    finding = first_finding(screen, (("a", "clean"), ("b", pem), ("c", pem)))

    assert finding == SecretFinding(rule=f"{POLICY_VERSION}/pem-block", field_path="b")


def test_first_finding_does_not_evaluate_fields_beyond_the_first_dirty_one() -> None:
    """Not merely an optimisation. The evidence payload is decoded lazily
    because it is a mebibyte wide, so a caller that materialised every field
    before screening would pay for a decode nobody reads."""

    def fields() -> Iterator[tuple[str, str]]:
        yield "a", "-----BEGIN RSA PRIVATE KEY-----"
        raise AssertionError("the second field was evaluated")

    assert first_finding(SecretScreen(), fields()) is not None


def test_first_finding_returns_the_lowest_sorted_rule_within_a_field() -> None:
    """A PEM block trips both the Cairn-owned rule and the upstream detector.
    ``screen`` sorts by ``(field_path, rule)``, so the caller is told about
    ``pem-block`` rather than about whichever rule happened to run first."""
    screen = SecretScreen()
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA\n"
        "-----END RSA PRIVATE KEY-----"
    )

    assert len(screen.screen("a", pem)) == 2
    finding = first_finding(screen, (("a", pem),))
    assert finding == SecretFinding(rule=f"{POLICY_VERSION}/pem-block", field_path="a")


def test_one_inserted_character_defeats_every_literal_rule() -> None:
    """The residual the Task 3 review surfaced, pinned so it cannot silently
    become false — or silently get worse. A hyphen, a space and a JSON escape
    all break a literal match equally; the escape is not a distinct weakness,
    which is the argument against adding an escape-decoding pass for it."""
    screen = SecretScreen()

    for evaded in (
        "AKIA-IOSFODNN7EXAMPLE",
        "AKIA IOSFODNN7EXAMPLE",
        "AKIA\\u0049OSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE\\u0020KEY-----",
    ):
        assert screen.screen("facts[0].body", evaded) == ()

    assert screen.screen("facts[0].body", "AKIAIOSFODNN7EXAMPLE") != ()


def test_contextual_entropy_still_backstops_an_escaped_secret() -> None:
    """Why the entry above calls the backstop real. The escape fractures the
    candidate run, but the tail is still sixteen characters of candidate
    charset inside the keyword window, so a labelled secret is still caught."""
    screen = SecretScreen()

    findings = screen.screen("facts[0].body", "password=AKIA\\u0049OSFODNN7EXAMPLE")

    assert [finding.rule for finding in findings] == [
        f"{POLICY_VERSION}/contextual-entropy"
    ]


# === I-97: the evidence-payload field-scoped rule matrix =====================
#
# The decoded exact-evidence payload is a verbatim historical record, so the
# three statistical rules are guaranteed false positives over it; the pattern
# rules keep running. Every literal below was run through the real
# ``SecretScreen`` before being written here.

_STATISTICAL_RULES = {
    f"{POLICY_VERSION}/contextual-entropy",
    f"{POLICY_VERSION}/upstream/Base64HighEntropyString",
    f"{POLICY_VERSION}/upstream/HexHighEntropyString",
}
_ENTROPY_ONLY_TEXT = (
    "the export token digest is "
    '"876131c8f232b000703776b76f1cfb1aca7fb11a8cc055cd360f413951144b64" '
    'and the marker is "R2c9k7Qw1Zx4Vb8Ln3Jm5Tp0Ys6Ue2Ia9Do4Hf1"'
)
_PATTERN_AND_ENTROPY_TEXT = (
    _ENTROPY_ONLY_TEXT + "\ndeploy log: export AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_the_statistical_rules_do_not_run_over_the_evidence_payload() -> None:
    """The guard proves the text trips exactly the three statistical rules
    when it arrives as a fact body; the same text as the decoded payload
    finds nothing."""
    assert (
        _rule_identities(SecretScreen().screen(_FIELD_PATH, _ENTROPY_ONLY_TEXT))
        == _STATISTICAL_RULES
    )
    assert SecretScreen().screen("evidence_payload", _ENTROPY_ONLY_TEXT) == ()


def test_the_evidence_payload_keeps_every_pattern_finding_a_body_would_get() -> None:
    body_rules = _rule_identities(
        SecretScreen().screen(_FIELD_PATH, _PATTERN_AND_ENTROPY_TEXT)
    )
    payload_rules = _rule_identities(
        SecretScreen().screen("evidence_payload", _PATTERN_AND_ENTROPY_TEXT)
    )
    assert payload_rules, "the pattern rules must still run over the payload"
    assert payload_rules == body_rules - _STATISTICAL_RULES


def test_the_exemption_is_the_evidence_payload_path_alone() -> None:
    """Every other screened field keeps the full rule set; the exemption
    must not leak to a path that merely contains the payload's name."""
    for field_path in (
        "facts[0].body",
        "metadata",
        "metadata[0].value",
        "reason",
        "query",
        "label",
        "evidence.external_uri",
        "evidence_payload[0]",
    ):
        assert (
            _rule_identities(SecretScreen().screen(field_path, _ENTROPY_ONLY_TEXT))
            == _STATISTICAL_RULES
        ), field_path
