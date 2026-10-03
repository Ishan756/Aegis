"""Incident report models.

A deployment failed. Something now has to say *why*, and the difference between
an investigation and a guess is entirely in whether the conclusions are tied to
observations that were actually made.

Three rules are encoded here rather than left to the agent that fills these in:

1. **A root cause must cite evidence.** :class:`SuspectedRootCause` refuses to
   validate with an empty ``evidence`` list. A cause nobody can point at is not a
   cause, it is a narrative, and a narrative that reaches an automated fix will
   eventually restart the wrong thing.
2. **Confidence is derived, not asserted.** Nothing sets ``confidence`` by
   opinion. :func:`derive_confidence` reads the evidence and the corroboration
   count, so "high confidence" means a specific, checkable thing rather than how
   confident the author felt.
3. **Unknown is a valid answer.** When nothing matches, the report says so. An
   incident report that must produce a cause will always produce one, which makes
   it worthless precisely when it matters.

The report is read-only. Producing one changes nothing.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.verification import Evidence

#: How much the evidence supports a conclusion.
Confidence = Literal["low", "medium", "high"]

#: Ordering used when comparing confidence levels.
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class FailureSignature(BaseModel):
    """A known failure shape, recognisable in logs without guesswork.

    ``component`` is what the evidence implicates, not what the operator assumes.
    A refused connection to ``postgres`` implicates the dependency, not the
    application that was trying to reach it.
    """

    id: str
    pattern: str
    cause: str
    component: str
    #: How much a single match is worth. A literal signature ("OOMKilled") is
    #: strong on its own; a vague one needs corroboration.
    strength: Literal["weak", "strong"] = "strong"
    #: What would confirm this beyond the log line itself.
    confirm: str = ""
    #: The fix category this signature implies, if any.
    fix_category: str | None = None


#: Signatures checked against log output and exit evidence, in priority order.
#:
#: Order matters: ``connection refused`` appears inside the oom and crash
#: messages too, so the specific causes are listed first and a broad pattern
#: cannot pre-empt them.
FAILURE_SIGNATURES: tuple[FailureSignature, ...] = (
    FailureSignature(
        id="out_of_memory",
        pattern=(
            r"\b(out of memory|oom[-_ ]?kill(?:ed)?|cannot allocate memory|"
            r"MemoryError|Killed process \d+)\b"
        ),
        cause="The process exceeded its memory limit and was killed.",
        component="application",
        confirm="Check the container's OOM flag and its memory limit against peak usage.",
        # No fix category, so the recommendation falls through to a restart. An
        # OOM is often a burst rather than a leak, and a restart is the cheapest
        # thing that could plausibly help without destroying anything.
        fix_category=None,
    ),
    FailureSignature(
        id="process_crashed",
        pattern=r"\b(panic:|Traceback \(most recent call last\)|segmentation fault|"
        r"fatal error:|Unhandled exception)",
        cause="The application terminated on an unhandled error.",
        component="application",
        confirm="Read the first exception in the log; everything after it is noise.",
        fix_category="code",
    ),
    FailureSignature(
        id="missing_configuration",
        pattern=r"\b(KeyError|Undefined variable|is not defined|"
        r"environment variable .{0,40}(missing|not set|required)|"
        r"required environment variable)\b",
        cause="Required configuration was absent at runtime.",
        component="configuration",
        confirm="Compare the variables the app reads against those the deployment sets.",
        fix_category="configuration",
    ),
    FailureSignature(
        id="dependency_refused",
        pattern=r"\b(ECONNREFUSED|connection refused|"
        r"could not connect to (?:server on )?(?:host|localhost|127\.0\.0\.1))",
        cause="A backing service is not accepting connections.",
        component="dependency",
        strength="weak",
        confirm="Check whether the dependency's container is running and listening.",
        fix_category="dependency",
    ),
    FailureSignature(
        id="dependency_missing",
        pattern=r"\b(no such host|unknown host|ENOTFOUND|nameserver|Temporary failure "
        r"in name resolution)\b",
        cause="A hostname could not be resolved.",
        component="configuration",
        strength="weak",
        confirm="Resolve the hostname from inside the same network namespace.",
        fix_category="configuration",
    ),
    FailureSignature(
        id="port_conflict",
        pattern=r"\b(EADDRINUSE|address already in use|bind: address already in use)\b",
        cause="The process could not bind because the port is already taken.",
        component="configuration",
        confirm="List what holds the port and whether the app expects to own it.",
        fix_category="configuration",
    ),
    FailureSignature(
        id="permission_denied",
        pattern=r"\b(permission denied|EACCES|operation not permitted)\b",
        cause="The process lacked permission for a file, port or syscall.",
        component="configuration",
        strength="weak",
        confirm="Check the user the image runs as against the resource it needs.",
        fix_category="configuration",
    ),
    FailureSignature(
        id="image_missing",
        pattern=r"\b(no such image|manifest unknown|repository does not exist|"
        r"pull access denied)\b",
        cause="The image could not be found or fetched.",
        component="build",
        confirm="List local images and confirm the tag the deployment asked for.",
        fix_category="build",
    ),
    FailureSignature(
        id="build_failed",
        pattern=r"\b(ERROR: (?:failed to solve|failed to compute cache key)|"
        r"Dockerfile parse error|unknown instruction)\b",
        cause="The image build failed.",
        component="build",
        confirm="Read the first Dockerfile line the builder named.",
        fix_category="build",
    ),
)


def match_signatures(text: str) -> list[tuple[FailureSignature, str]]:
    """Return every signature present in ``text``, strongest first.

    Deduplicated by signature id, keeping the first matching line, so a cause
    repeated forty times in a log is reported once.
    """
    if not text:
        return []

    found: dict[str, tuple[FailureSignature, str]] = {}
    for signature in FAILURE_SIGNATURES:
        for line in text.splitlines():
            if re.search(signature.pattern, line, re.IGNORECASE):
                found.setdefault(signature.id, (signature, line.strip()[:300]))
                break

    ordered = sorted(
        found.values(),
        key=lambda item: (item[0].strength != "strong", item[0].id),
    )
    return ordered


class Observation(BaseModel):
    """One thing that was looked at, and what it showed.

    ``source`` names the tool, so a reader can tell a measurement from an
    inference. ``supports`` names the cause identifiers this observation backs,
    which is what makes corroboration checkable rather than claimed.
    """

    source: str = Field(description="Tool or field this came from.")
    detail: str = ""
    excerpt: str | None = Field(default=None, description="Short verbatim evidence.")
    supports: list[str] = Field(
        default_factory=list, description="Root cause ids this observation backs."
    )

    def summary(self) -> str:
        parts = [self.source]
        if self.detail:
            parts.append(self.detail)
        if self.supports:
            parts.append(f"supports={','.join(self.supports)}")
        return " | ".join(parts)


class SuspectedRootCause(BaseModel):
    """A candidate cause, with the observations that suggest it."""

    id: str
    cause: str
    component: str
    evidence: list[Observation] = Field(default_factory=list)
    #: Filled by :func:`derive_confidence`, never set by hand.
    confidence: Confidence = "low"
    confirm: str = Field(
        default="", description="What would confirm this beyond what is already known."
    )
    fix_category: str | None = None

    @model_validator(mode="after")
    def _require_evidence(self) -> SuspectedRootCause:
        if not self.evidence:
            raise ValueError(
                f"root cause '{self.id}' cites no evidence. A cause nobody can point "
                "at is a guess, and a guess that reaches an automated fix eventually "
                "restarts the wrong thing."
            )
        return self


class FixRecommendation(BaseModel):
    """A proposed remedy. Applying it is a separate, separately gated decision."""

    #: Machine-readable action id, e.g. ``restart_container``.
    action: str
    description: str
    rationale: str
    #: The MCP tool this would call, if any. Investigation never calls it.
    tool: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    requires_approval: bool = True
    reversible: bool = False
    #: True when the proposal is only a proposal and no automated path exists.
    manual_only: bool = False

    def summary(self) -> str:
        return f"{self.action}: {self.description}"


class IncidentReport(BaseModel):
    """What broke, what the evidence says, and what to do about it.

    ``automatic_fix_safe`` is the field the self-healing loop reads first, and it
    is false unless a cause reached at least ``medium`` confidence. A report whose
    evidence does not support a cause is a correct and useful report; it simply
    does not authorise an automated action.
    """

    incident_id: str = Field(description="Stable identifier for this incident.")

    symptom: str = Field(description="What was observed to be wrong, in plain terms.")
    evidence: list[Observation] = Field(default_factory=list)
    suspected_root_causes: list[SuspectedRootCause] = Field(default_factory=list)
    confidence: Confidence = "low"
    affected_component: str = Field(
        default="unknown", description="build, image, container, application, dependency, ..."
    )
    recommended_fix: FixRecommendation | None = None
    automatic_fix_safe: bool = False
    next_action: str = Field(description="The single next thing a human should do.")

    #: Where the deployment was expected to come from, for commit correlation.
    repository: str | None = None
    investigated_at: str | None = None
    #: True when the investigation ran but found nothing conclusive. Reported
    #: honestly instead of producing a low-quality cause to fill the field.
    inconclusive: bool = False

    @model_validator(mode="after")
    def _confidence_matches_evidence(self) -> IncidentReport:
        """Keep the headline confidence consistent with the causes behind it.

        A report claiming high confidence while listing no causes is the exact
        failure mode this module exists to prevent, so it is rejected rather than
        reported.
        """
        if self.confidence == "high" and not self.suspected_root_causes:
            raise ValueError(
                "confidence 'high' requires at least one suspected root cause; "
                "confidence that is not backed by a cause is optimism, not evidence"
            )
        if self.automatic_fix_safe and self.confidence == "low":
            raise ValueError(
                "automatic_fix_safe cannot be true while confidence is low. Low "
                "confidence exists to stop the loop, not to be overridden by it."
            )
        return self

    @property
    def primary_cause(self) -> SuspectedRootCause | None:
        """The best-supported cause, or None when nothing was conclusive."""
        if not self.suspected_root_causes:
            return None
        return max(
            self.suspected_root_causes,
            key=lambda cause: CONFIDENCE_ORDER[cause.confidence],
        )


def derive_confidence(
    observations: list[Observation],
    *,
    strength: Literal["weak", "strong"] = "strong",
) -> Confidence:
    """Derive a confidence level from what corroborates a cause.

    The rule is deliberately mechanical so it cannot be talked up:

    - **high** — two or more observations from *different* sources. One log line
      repeating does not corroborate itself.
    - **medium** — a single strong signature, or one observation.
    - **low** — a weak signature, or nothing.

    Corroboration is counted by distinct source. An operator who greps one log
    file forty times has one source, not forty.
    """
    distinct = {observation.source for observation in observations}

    if len(distinct) >= 2:
        return "high"
    if strength == "strong" and len(distinct) == 1:
        return "medium"
    return "low"


def evidence_from_verification(evidence: list[Evidence]) -> list[Observation]:
    """Convert verification evidence into incident observations."""
    return [
        Observation(source=item.source, detail=item.detail, excerpt=item.excerpt)
        for item in evidence
    ]


__all__ = [
    "CONFIDENCE_ORDER",
    "FAILURE_SIGNATURES",
    "Confidence",
    "FailureSignature",
    "FixRecommendation",
    "IncidentReport",
    "Observation",
    "SuspectedRootCause",
    "derive_confidence",
    "evidence_from_verification",
    "match_signatures",
]
