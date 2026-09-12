"""The pinned ``detect-secrets`` detector set of I-75 and its parity helper.

I-31 takes the distribution for its pattern and entropy detectors while
rejecting its baseline and CLI workflow, so nothing here reads or writes a
repository baseline and nothing consults ``detect_secrets.settings``. Plugins
are constructed directly with the settings P-25 pins, restated below as
literals, which is what stops a change to an upstream default quietly altering
a policy still calling itself ``cairn.secret/v1``.

Detection uses ``analyze_line`` rather than ``analyze_string``, and the
difference is not cosmetic. ``HighEntropyStringsPlugin.analyze_string``
deliberately skips the Shannon check and defers it to ``analyze_line`` - its
own comment says so - so scanning strings would report every sufficiently long
quoted value as a secret and discard the pinned entropy limits entirely. The
plugins are line-oriented by design, so they take pre-split lines — split once
by ``SecretScreen.screen`` and shared, rather than re-split by each of the
twenty-six matchers per field.

``verify()`` is the library's network path, and I-31 requires screening to
complete before any remote dependency call. It is not merely unused: every
plugin's method is replaced at construction, because ``analyze_line`` reaches
it through ``detect_secrets``' process-wide settings, which are global mutable
state this package does not own.

Findings never carry what a detector matched. ``analyze_line`` returns
``PotentialSecret`` values that hold the matched text, so its result is used
only for its truth value and is discarded immediately, per I-31.

Stated residual: a secret split across two real newlines is not detected,
because these detectors are line-oriented and so is this scan. That is
inherent to the upstream design - its own CLI behaves the same way - rather
than a Cairn choice. Splitting on anything other than a real newline is a
different matter and is not done; see ``SecretScreen.screen``.
"""

from collections.abc import Callable, Mapping
from typing import NoReturn

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.plugins.base import BasePlugin
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HexHighEntropyString,
)
from detect_secrets.plugins.keyword import KeywordDetector

# A public IPv4 address is not a secret, and rejecting every fact that mentions
# one would make Cairn useless for operational memory (I-75).
EXCLUDED_DETECTOR = "IPPublicDetector"

# The pinned inventory: the distribution's full detector set minus the
# exclusion above, enumerated from the installed package on 6 August 2026 and
# stated here in I-75's own order. The parity helper compares this against what
# is actually installed, so an upstream addition or removal fails CI rather
# than silently redefining the policy.
PINNED_DETECTORS: tuple[str, ...] = (
    "ArtifactoryDetector",
    "AWSKeyDetector",
    "AzureStorageKeyDetector",
    "Base64HighEntropyString",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "HexHighEntropyString",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "KeywordDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "PypiTokenDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TelegramBotTokenDetector",
    "TwilioKeyDetector",
)

# P-25: the distribution's defaults, restated as literals beside the constant
# so that an upstream change to any of them is a visible diff here rather than
# a silent change of policy. Verified against detect-secrets 1.5.0.
HEX_ENTROPY_LIMIT = 3.0
BASE64_ENTROPY_LIMIT = 4.5
KEYWORD_EXCLUDE: str | None = None

# analyze_line requires a filename, and a caller's field path must not travel
# into a third-party library, so it is a constant. It is not inert:
# KeywordDetector passes it to determine_file_type and selects a different
# regex family per extension. "cairn" has no extension, so it resolves to
# FileType.OTHER and the default family, which a test pins - adding a dot to
# this value would silently change which secrets are found.
_FILENAME = "cairn"


def _forbid_verification(*args: object, **kwargs: object) -> NoReturn:
    """Fail closed if the library's network path is ever reached.

    ``BasePlugin.analyze_line`` calls ``self.verify`` when a particular filter
    is present in ``detect_secrets``' process-wide settings. That key is not in
    the distribution's defaults and Cairn never configures those settings, but
    they are global mutable state shared with a third-party package, so
    "``verify`` is never called" would otherwise be contingent rather than
    structural. Overriding the method makes it structural, and raising rather
    than passing means a settings change fails loudly instead of quietly
    turning screening into an outbound HTTP call.
    """
    raise RuntimeError(
        "detect-secrets verification is the library's network path; I-31 "
        "requires screening to complete before any remote dependency call"
    )


def _classes() -> dict[str, type[BasePlugin]]:
    # The mapping consults detect_secrets' global settings and will import
    # custom plugins from any path declared there. Cairn never populates those
    # settings, so only the distribution's own detectors appear - and if
    # anything ever did inject one, the parity test would fail rather than the
    # policy silently gaining a rule.
    # Mapping rather than dict: the upstream annotation carries an unbound type
    # variable, which mypy resolves to Never, and dict is invariant in its
    # value type where Mapping is covariant.
    mapping: Mapping[str, type[BasePlugin]] = get_mapping_from_secret_type_to_class()
    return {detector.__name__: detector for detector in mapping.values()}


def installed_detectors() -> frozenset[str]:
    """Every detector class name the installed distribution provides."""
    return frozenset(_classes())


def _construct(name: str) -> BasePlugin:
    plugin: BasePlugin
    if name == "Base64HighEntropyString":
        plugin = Base64HighEntropyString(limit=BASE64_ENTROPY_LIMIT)
    elif name == "HexHighEntropyString":
        plugin = HexHighEntropyString(limit=HEX_ENTROPY_LIMIT)
    elif name == "KeywordDetector":
        plugin = KeywordDetector(keyword_exclude=KEYWORD_EXCLUDE)
    else:
        plugin = _classes()[name]()
    plugin.verify = _forbid_verification  # type: ignore[method-assign]
    return plugin


def _matcher(plugin: BasePlugin) -> Callable[[tuple[str, ...]], bool]:
    def matches(lines: tuple[str, ...]) -> bool:
        # The caller splits once and every plugin shares the lines; the ruling
        # on how to split - real newlines only, never splitlines() - lives at
        # the single split site, ``SecretScreen.screen``.
        return any(
            plugin.analyze_line(filename=_FILENAME, line=line, line_number=1)
            for line in lines
        )

    return matches


# Built once at import. A name in PINNED_DETECTORS that the installed
# distribution does not provide raises here rather than degrading quietly to a
# policy with a missing rule. Private, so that BasePlugin - a third-party type -
# stays inside this module; DETECTOR_RULES below is the contained boundary the
# rest of the package sees.
_PLUGINS: tuple[tuple[str, BasePlugin], ...] = tuple(
    (name, _construct(name)) for name in PINNED_DETECTORS
)

DETECTOR_RULES: tuple[tuple[str, Callable[[tuple[str, ...]], bool]], ...] = tuple(
    (f"upstream/{name}", _matcher(plugin)) for name, plugin in _PLUGINS
)
