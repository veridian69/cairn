"""The ``cairn.secret/v1`` policy and the six Cairn-owned rules of I-75.

The screen is pure (P-24): text and a field path in, findings out, with no
clock, no randomness, no storage, no logging and no state carried between
calls. It imports neither transport nor catalogue code, so the policy cannot
come to depend on where the text arrived from.

A finding names a rule identity and the field path it was handed, and nothing
else. I-31 forbids disclosing the matched text, its offsets or its
surrounding context, so a finding cannot leak what it found even when it is
mishandled downstream. That also collapses repeated matches of one rule into
a single finding: without an offset, two would be the same value twice.

These six are Cairn's own. The pinned ``detect-secrets`` detectors of I-75
arrive separately and surface as ``cairn.secret/v1/upstream/<PluginClassName>``.
The two sets are complementary by intent rather than overlapping, which is why
``provider-token`` carries the vendors that distribution omits instead of
repeating the ones it already has.

Stated residuals, accepted rather than undiscovered:

- **Prose false positives.** ``contextual-entropy`` fires when a keyword and an
  unrelated high-entropy string fall within the window, so "the password
  rotation script uses commit <sha>" is rejected. Accepted on I-31's posture:
  there is no baseline, no per-request exemption and no privileged bypass, and
  a caller must redact or reformulate a false positive. A false negative writes
  a credential into permanent custody; a false positive costs a rewording.
- **Unicode homoglyphs.** Text is NFKC-folded and stripped of format
  characters before screening, which closes an invisible inserted into a
  literal, a soft hyphen inside a token and a fullwidth transliteration. Two
  cases remain. A homoglyph — Cyrillic ``А`` for Latin ``A`` — survives,
  because NFKC does not touch it and folding confusables needs a table this
  slice may not add a dependency for. And a format character *substituted for*
  a required space leaves the neighbouring words joined, so ``PRIVATE`` and
  ``KEY`` still fail to match.

  Declined deliberately rather than deferred: no content-inspection screen
  stops a determined encoder, who would base64 the secret, reverse it, or
  split it across two facts long before reaching for Cyrillic. The threat this
  fold actually buys down is the accidental one — a key pasted from a web page
  or a PDF, carrying invisible characters with it — and that case is now
  covered. Deliberate exfiltration is answered by scope isolation, candidate-
  only worker writes and independent verifier lineage, not by this module.
- **One inserted character.** Every literal-matching rule here breaks on a
  single character inserted into the secret: ``AKIA-IOSFODNN7EXAMPLE``,
  ``AKIA IOSFODNN7EXAMPLE`` and ``AKIA\\u0049OSFODNN7EXAMPLE`` all pass where
  the unbroken key does not, and a ``\\u0020`` standing in for the space in a
  PEM header defeats ``pem-block`` the same way. ``contextual-entropy`` is the
  backstop and a real one — it still fires on the escaped form when a keyword
  is within the window — but it is only a backstop, because without a nearby
  keyword nothing fires at all.

  Named here because the entry below on determined encoders reads as though
  evasion costs effort, and this costs one keystroke. The backslash-escape
  case is the tidiest of the family, being losslessly recoverable by an
  ordinary JSON decode, but it is not a distinct weakness and no
  escape-decoding pass is planned: decoding ``\\uXXXX`` would leave
  ``%49``, ``&#73;`` and every other encoding untouched, and the line has to
  fall somewhere. It falls where I-31 puts it — at scope isolation and
  candidate-only writes, not at the screen.

  Free-text fields only. ``metadata`` is immune because
  ``_validate_metadata`` refuses anything that is not already its own
  canonical re-serialisation, so an escaped literal is rejected as
  ``invalid_metadata`` before the screen runs and only the decoded form ever
  reaches it. ``label`` and ``reason_code`` are immune because their
  grammars admit no backslash.
- **Patternless secrets in exact evidence.** I-97 (ruled 23 August 2026): the
  three statistical rules — ``contextual-entropy`` and the upstream
  ``Base64HighEntropyString`` / ``HexHighEntropyString`` — do not run over
  the ``evidence_payload`` field, so a high-entropy secret with no
  recognisable pattern (a bare random API key carrying no marker prefix)
  passes inside an evidence payload where every other field refuses it. A
  genuine weakening of one field's screen, priced rather than hidden, and
  traded for the ability to hold verbatim evidence at all: an exact-evidence
  record is by definition verbatim history, and the statistical detectors
  are guaranteed false positives over its identifiers, digests and path
  slugs. Every pattern rule still runs there, and the alternatives were
  worse — redacting evidence breaks the meaning of "exact", and a
  migration-principal exemption is exactly the privileged bypass I-31
  forbids.
- **Short credentials.** ``authorization-header`` ignores values under sixteen
  characters so that placeholders survive, so a genuinely short token slips it.
- **Line-wrapped URI passwords.** ``credential-uri`` stops at whitespace, so a
  password broken across a terminal line wrap is not matched.
- **``sk-proj-`` keys.** The generic ``sk-`` shape requires an unbroken
  alphanumeric run, which ``sk-proj-`` breaks after four characters. Closed
  rather than residual: the upstream ``OpenAIDetector`` keys off the
  ``T3BlbkFJ`` marker instead of the prefix, so the shape is covered, and a
  test in ``tests/screening/test_upstream.py`` pins that it stays covered.
"""

import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cairn.screening.upstream import DETECTOR_RULES

POLICY_VERSION = "cairn.secret/v1"

PEM_BLOCK = f"{POLICY_VERSION}/pem-block"
AUTHORIZATION_HEADER = f"{POLICY_VERSION}/authorization-header"
CREDENTIAL_URI = f"{POLICY_VERSION}/credential-uri"
PROVIDER_TOKEN = f"{POLICY_VERSION}/provider-token"
CONTEXTUAL_ENTROPY = f"{POLICY_VERSION}/contextual-entropy"
CAIRN_TOKEN = f"{POLICY_VERSION}/cairn-token"

CAIRN_RULES = frozenset(
    {
        AUTHORIZATION_HEADER,
        CAIRN_TOKEN,
        CONTEXTUAL_ENTROPY,
        CREDENTIAL_URI,
        PEM_BLOCK,
        PROVIDER_TOKEN,
    }
)

# I-75 surfaces every pinned detect-secrets detector under its own identity, so
# a finding names which detector fired rather than hiding behind one collective
# "upstream" rule.
UPSTREAM_RULES = frozenset(f"{POLICY_VERSION}/{suffix}" for suffix, _ in DETECTOR_RULES)
ALL_RULES = CAIRN_RULES | UPSTREAM_RULES

# I-97: the decoded exact-evidence payload is screened by the pattern rules
# only. An exact-evidence payload is by definition a verbatim historical
# record, and statistical detectors calibrated for source-code-like lines are
# guaranteed false positives over arbitrary verbatim records carrying
# identifiers, digests and path slugs — the take2 migration corpus measured
# the guarantee. The exemption is the exact field path the custody seam
# yields for the payload and no other; rule identities, rule semantics and
# the policy version are unchanged, which is why this is a field matrix here
# rather than a second policy.
EVIDENCE_PAYLOAD_FIELD = "evidence_payload"
STATISTICAL_RULES = frozenset(
    {
        CONTEXTUAL_ENTROPY,
        f"{POLICY_VERSION}/upstream/Base64HighEntropyString",
        f"{POLICY_VERSION}/upstream/HexHighEntropyString",
    }
)

# Private-key-bearing labels only. A CERTIFICATE or PUBLIC KEY block is public
# by construction, and rejecting facts that quote one would cost Cairn the
# operational memory it exists to hold — the reasoning that removed
# IPPublicDetector from the pinned upstream set. The closing marker is not
# required, because a truncated paste has still leaked the key, and case is
# ignored, because one retyped in lower or mixed case has leaked just as
# thoroughly while matching uppercase alone would be a no-skill evasion.
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----", re.IGNORECASE
)

# The scheme word is optional and unconstrained, so a header carrying a bare
# credential is caught alongside Bearer, Basic and Token. Sixteen characters of
# credential charset is the floor: below it, placeholders such as <token> and
# REDACTED pass, which is deliberate. The value must also contain a digit,
# because the charset admits ``-``, ``_`` and ``.`` — hyphenated placeholders
# (``your-token-goes-here``) and this codebase's own reason codes are sixteen
# characters of it with no digit anywhere, while a real credential without one
# is vanishingly rare. The stated residuals are a real token shorter than
# sixteen characters and a digit-free one; the upstream detectors and
# contextual entropy (``bearer`` is a keyword) remain in front of both.
# Whitespace is ``\s`` rather than space-and-tab so a value folded onto a
# continuation line, or separated by a non-breaking space from a copied
# terminal capture, is still caught. ``%`` is in the value charset because a
# percent-encoded credential would otherwise fragment into two-character runs
# and evade this rule and contextual entropy at the same time - a blind spot
# across the whole policy rather than one degraded rule. The header word
# forbids a preceding letter for the same reason the provider tokens forbid a
# preceding alphanumeric: without it the rule fires inside ``preauthorization``
# and ``reauthorization``, making an ordinary reference number after either
# word unwritable. The hyphenated spellings need their own lookbehinds —
# ``pre-authorization:`` is the same operational prose with a hyphen the
# letter bar cannot see — and they are spelled out rather than barring ``-``
# wholesale because ``X-Authorization`` is a real custom header carrying a
# real credential and must keep firing. ``proxy-`` stays admitted explicitly.
# ``(?<!re-)`` bars ``pre-`` as well — the three characters preceding the
# match are ``re-`` in both spellings.
_AUTHORIZATION_HEADER_VALUE = re.compile(
    r"(?<![A-Za-z])(?<!re-)(?<!de-)(?:proxy-)?authorization\s*:\s*"
    r"(?:[A-Za-z][A-Za-z0-9-]*\s+)?"
    r"(?=[A-Za-z+/=._~%-]*[0-9])[A-Za-z0-9+/=._~%-]{16,}",
    re.IGNORECASE,
)

# Both components must be non-empty: https://user@host carries no credential,
# and postgres://user:@host carries an empty one. The delimiter admits its
# percent-encoded form, because encoding only the colon leaves a URI every real
# client still resolves while defeating a literal-colon match. The password
# charset admits ``:`` where the username's does not: real clients split
# userinfo on the first colon only, so ``p4:ssw0rd`` is a legal raw DSN
# password, and excluding the colon from both components let exactly that
# password through unscreened.
_CREDENTIAL_URI_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@]+(?::|%3A)[^\s/?#@]+@",
    re.IGNORECASE,
)

# The vendors detect-secrets 1.5.0 does not cover. Deliberately shapes rather
# than a catalogue: I-31 requires a versioned release and a rescan to change
# policy, so a rule that tracked a vendor list would age badly between them.
# Every shape forbids a preceding alphanumeric: with IGNORECASE and no left
# boundary they match inside ordinary hyphenated identifiers —
# ``RISK-ASSESSMENT2026REPORTX`` carries ``SK-`` and twenty-one alphanumerics,
# ``RISK-ANT-ASSESSMENT-2026-REPORT-XYZAB`` carries ``SK-ANT-`` — and a caller
# cannot redact an identifier that carries no secret. A real token is never
# immediately preceded by a letter or digit. The lookbehind is on all four
# rather than the generic shape alone: applying it to one sibling and not the
# rest left the same defect standing in three patterns.
_PROVIDER_TOKENS = (
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
)

# I-62's token text format, matched anywhere in the text rather than anchored:
# a leaked credential is usually quoted inside a sentence. Cairn must not be a
# route around its own token hygiene. This mirrors
# ``cairn.authority.credentials.TOKEN_PATTERN`` character for character and is
# deliberately not imported from it: that module reaches into the catalogue,
# and the purity constraint above forbids the dependency. The copies are kept
# honest by a test that mints a token through the real credential path and
# requires this screen to catch it, so neither can drift alone. Case is ignored
# where I-62 does not ignore it: a token whose UUID has been upper-cased - by a
# Windows GUID convention, a logging framework, or ``str(u).upper()`` - will not
# authenticate, but its secret component has leaked all the same.
_CAIRN_TOKEN_PATTERN = re.compile(
    r"cairn1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"\.[A-Za-z0-9_-]{43}",
    re.IGNORECASE,
)

# Deliberately tight. Bare "key" and bare "auth" are excluded: they appear in
# ordinary operational prose beside ordinary high-entropy strings — "the cache
# key is <digest>" — and including them would reject exactly the facts an
# engineer most wants to keep. The lookarounds stop "secret" matching inside
# "secretary" while still matching inside "client_secret".
#
# Every entry earns its place. A compound whose tail is itself a keyword is
# *not* listed: the window runs forward from the match end, and "client_secret"
# ends where the "secret" inside it ends, so listing it changes no window —
# "auth_token", "client_secret" and "secret_key" were all removed on that
# reasoning, the last of them additionally unreachable because the alternation
# selects "secret" first and the lookahead passes on the underscore. The
# "<word>_key" entries are the opposite case and stay: bare "key" is excluded
# deliberately, so nothing else would match them.
_ENTROPY_KEYWORDS = (
    "access_key",
    "api-key",
    "api_key",
    "apikey",
    "bearer",
    "credential",
    "credentials",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
_ENTROPY_KEYWORD = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(_ENTROPY_KEYWORDS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_%-]{16,}")
_HEX_CANDIDATE = re.compile(r"[0-9a-fA-F]+")

# A UUID is not a secret. I-74 already declines to screen UUID-typed fields for
# that reason, and a UUID written into free text is the same value wearing no
# disguise - but its hyphens fall inside the candidate charset, so the whole
# thing reads as one high-entropy run and would otherwise fire beside any
# keyword. "credential ID: 3fa85f64-..." is a note, not a leak.
_UUID_SHAPED = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# The window runs forward from the keyword only: secrets follow their label far
# more often than they precede it, and a symmetric window doubles the false
# positives for no gain. Both floors sit below the upstream raw limits (hex
# 3.0, base64 4.5) so this rule catches what raw entropy alone misses. The real
# discriminator is keyword proximity rather than the floor — a genuine token
# and a commit digest both sit near 3.8 — which is why the keyword list above
# is the load-bearing part of this rule.
_WINDOW = 64
_BASE64_ENTROPY_FLOOR = 3.5
_HEX_ENTROPY_FLOOR = 2.5


def _shannon_entropy(text: str) -> float:
    counts = Counter(text)
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _contains_pem_block(text: str) -> bool:
    return _PEM_PRIVATE_KEY.search(text) is not None


def _contains_authorization_header(text: str) -> bool:
    return _AUTHORIZATION_HEADER_VALUE.search(text) is not None


def _contains_credential_uri(text: str) -> bool:
    return _CREDENTIAL_URI_PATTERN.search(text) is not None


def _contains_provider_token(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _PROVIDER_TOKENS)


def _contains_cairn_token(text: str) -> bool:
    return _CAIRN_TOKEN_PATTERN.search(text) is not None


def _candidate_trips(value: str) -> bool:
    # UUIDs are excised wherever they sit, not only when they are the whole
    # candidate: ``credential=<uuid>`` reads as one run because ``=`` and
    # ``_`` are in the candidate charset, and a fullmatch test would judge
    # the joined value's entropy with the exemption never applying — the
    # I-74 reasoning (a UUID is not a secret) does not stop holding because
    # a separator touched it. What remains after excision is judged on its
    # own: below the sixteen-character floor it could never have been a
    # candidate, and above it a genuine secret sharing a run with a UUID is
    # still caught on its non-UUID content.
    stripped = _UUID_SHAPED.sub("", value)
    if len(stripped) < 16:
        return False
    entropy = _shannon_entropy(stripped)
    if entropy >= _BASE64_ENTROPY_FLOOR:
        return True
    return (
        _HEX_CANDIDATE.fullmatch(stripped) is not None and entropy >= _HEX_ENTROPY_FLOOR
    )


def _contains_contextual_entropy(text: str) -> bool:
    # Candidates are found across the whole text and then filtered by position,
    # rather than by slicing the window out first. Slicing truncates any
    # candidate straddling the boundary, which distorts its entropy and leaves a
    # partial UUID that no longer matches the shape excluded below.
    #
    # The test is overlap rather than containment. A keyword's own letters share
    # the candidate charset, so "password=SECRET" is a single run beginning
    # before the keyword ends; requiring the run to start after it would miss
    # the commonest shape this rule exists to catch.
    #
    # The candidate scan is deferred until the first keyword: keyword-free text
    # — the common clean case — pays nothing beyond the keyword search. Each
    # keyword then bisects into the candidates overlapping its own window
    # instead of scanning all of them, and each candidate's verdict is computed
    # at most once however many windows it falls in. Both bounds matter:
    # keyword-dense text was otherwise quadratic in caller-controlled input,
    # which an authorised caller could turn into minutes of CPU per request.
    candidates: tuple[re.Match[str], ...] = ()
    ends: list[int] = []
    starts: list[int] = []
    verdicts: list[bool | None] = []
    for keyword in _ENTROPY_KEYWORD.finditer(text):
        if not candidates:
            candidates = tuple(_ENTROPY_CANDIDATE.finditer(text))
            if not candidates:
                return False
            ends = [candidate.end() for candidate in candidates]
            starts = [candidate.start() for candidate in candidates]
            verdicts = [None] * len(candidates)
        # Overlap means end > keyword.end() and start < keyword.end() + window;
        # matches are disjoint and ordered, so both lists are sorted.
        first = bisect_right(ends, keyword.end())
        last = bisect_left(starts, keyword.end() + _WINDOW)
        for index in range(first, last):
            verdict = verdicts[index]
            if verdict is None:
                verdict = _candidate_trips(candidates[index].group())
                verdicts[index] = verdict
            if verdict:
                return True
    return False


# Two groups with one iteration order between them. The Cairn-owned rules
# match over the folded text; the upstream detectors are line-oriented and
# take the lines ``screen`` splits exactly once, rather than each of the
# twenty-six re-splitting the same field. Both groups are sorted by rule
# identity at build — and every ``cairn.secret/v1/<name>`` sorts before every
# ``cairn.secret/v1/upstream/<Name>`` — so ``screen`` emits findings already
# in the documented order without sorting per call. The cross-group ordering
# is pinned by ``test_findings_are_sorted_by_field_path_then_rule``, which
# fires rules from both groups; an assert here was dropped because ``-O``
# strips it, which is a guard that only pretends to guard.
_TEXT_RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    (AUTHORIZATION_HEADER, _contains_authorization_header),
    (CAIRN_TOKEN, _contains_cairn_token),
    (CONTEXTUAL_ENTROPY, _contains_contextual_entropy),
    (CREDENTIAL_URI, _contains_credential_uri),
    (PEM_BLOCK, _contains_pem_block),
    (PROVIDER_TOKEN, _contains_provider_token),
)
_LINE_RULES: tuple[tuple[str, Callable[[tuple[str, ...]], bool]], ...] = tuple(
    sorted(
        ((f"{POLICY_VERSION}/{suffix}", matches) for suffix, matches in DETECTOR_RULES),
        key=lambda pair: pair[0],
    )
)


# Every Unicode format character (general category Cf), pinned as ranges rather
# than swept at import: scanning the whole codepoint space costs about 43ms,
# which is not a price to pay on every process start. A test compares this
# constant against a live scan, the same shape as the P-25 detector parity
# check, so a Python upgrade that adds a format character fails CI instead of
# quietly leaving a hole.
_FORMAT_CHARACTER_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

_FORMAT_CHARACTERS: dict[int, None] = dict.fromkeys(
    codepoint
    for low, high in _FORMAT_CHARACTER_RANGES
    for codepoint in range(low, high + 1)
)


def normalise_for_screening(text: str) -> str:
    """Fold a screening copy of the text; the caller's own bytes are untouched.

    Public because the custody seam's I-96 boundary decision must be made
    over the same rendering the screen judges (the re-review finding over
    `db8b510`: a format separator or a fullwidth prefix made a constructed
    credential look standalone to a raw-text boundary). Format characters
    are stripped *before* NFKC runs: a format character has combining class
    0 and blocks canonical composition, so normalising first can leave a
    composition boundary the strip then exposes, and a second fold would
    compose it (the closure-review finding over ``c9ccda2``: A + U+200B +
    U+030A). Stripping first makes the fold idempotent — NFKC emits no
    format character (pinned by test against the running Unicode database)
    and is idempotent itself — so a seam that pre-folds hands ``screen``
    text the screen's own fold leaves unchanged.

    ASCII short-circuits, and not merely as an optimisation: ASCII is already
    NFKC-normal and the lowest format character is U+00AD, so the transform is
    provably the identity there. It is also the common case, which is why a
    2 MiB request costs nothing to pass through.

    I-30 forbids normalising *stored* content, and this does not: the fold
    exists only to be pattern-matched and is then discarded, so exact evidence
    still keeps the decoded bytes it arrived with.
    """
    if text.isascii():
        return text
    return unicodedata.normalize("NFKC", text.translate(_FORMAT_CHARACTERS))


@dataclass(frozen=True, slots=True)
class SecretFinding:
    rule: str
    field_path: str


class SecretScreen:
    """The ``cairn.secret/v1`` screen over one caller-authored text field.

    Every rule runs on every call. Short-circuiting on the first match would
    make the result depend on rule order, which a later rule could silently
    disturb; running them all costs a second pass over text already bounded by
    I-30 and keeps a redacting caller told about everything it must fix.

    One field is the exception, by ruling rather than by drift: the
    ``evidence_payload`` path skips the ``STATISTICAL_RULES`` per I-97. The
    field path is already this method's argument, so the matrix lives here —
    where the policy is — rather than at each seam.
    """

    def screen(self, field_path: str, text: str) -> tuple[SecretFinding, ...]:
        exempt = (
            STATISTICAL_RULES
            if field_path == EVIDENCE_PAYLOAD_FIELD
            else frozenset[str]()
        )
        candidate = normalise_for_screening(text)
        # split("\n"), never splitlines(). splitlines() also breaks on \v, \f,
        # \x1c-\x1e, \x85, U+2028 and U+2029, every one of which regex ``\s``
        # matches - so a detector whose pattern tolerates whitespace between a
        # keyword and its value would bridge the character while the scan had
        # already cut the line in two. One invisible byte would have smuggled a
        # password past KeywordDetector. Splitting on real newlines only keeps
        # more text in front of each pattern, never less.
        lines = tuple(candidate.split("\n"))
        # Already in the documented (field_path, rule) order: the field path is
        # this call's single argument, and the rule groups are sorted at build.
        findings = [
            SecretFinding(rule=rule, field_path=field_path)
            for rule, matches in _TEXT_RULES
            if rule not in exempt and matches(candidate)
        ]
        findings.extend(
            SecretFinding(rule=rule, field_path=field_path)
            for rule, matches in _LINE_RULES
            if rule not in exempt and matches(lines)
        )
        return tuple(findings)


def first_finding(
    screen: SecretScreen, fields: Iterable[tuple[str, str]]
) -> SecretFinding | None:
    """The first finding over ``fields`` in the order given, or ``None``.

    Deliberately short-circuiting, and deliberately *not* the same rule as
    ``screen`` itself: within one field every rule always runs, because rule
    order must not decide the answer, but across fields the seam stops at the
    first dirty one. Only the first finding reaches the response, and a
    maximum ingest carries 6.25 MiB of fact bodies, so scanning the rest after
    the answer is settled buys the caller nothing.

    The order of ``fields`` is therefore part of each caller's behaviour, not
    an incidental iteration order.
    """
    for field_path, text in fields:
        findings = screen.screen(field_path, text)
        if findings:
            return findings[0]
    return None


def audit_reason_code(rule: str) -> str:
    """Map a rule identity onto the closed ``secret_<stem>`` audit reason code.

    The frozen ``cairn.audit/v1`` value has no field for a rule identity, and
    reopening I-54 is out of scope, so P-26 carries the rule in the denial
    event's ``reason_code`` instead. Derived rather than tabulated: a
    hand-written mapping would be a second copy of the rule vocabulary and
    could drift from it silently. The full 32-entry enumeration is asserted
    literally in ``tests/screening/test_policy.py``, which is where drift has
    to fail — the same shape as the P-25 detector parity check.

    Every result satisfies ``cairn.catalogue.audit``'s reason-code grammar:
    lowercase, underscore-separated, and at 40 characters for the longest —
    ``secret_upstream_telegrambottokendetector`` — well inside its
    63-character ceiling.
    """
    stem = rule.removeprefix(f"{POLICY_VERSION}/")
    return f"secret_{stem.replace('/', '_').replace('-', '_').lower()}"
