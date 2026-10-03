"""Deployment verification contracts.

A deployment that reports success has only proven that nothing errored. These
contracts check the things that actually matter afterwards: the container exists,
it is still running, the port answers, the health endpoint returns the status the
application promises, the logs are not full of fatal errors, and the services it
depends on are reachable.

Three states, not two. ``WARNING`` exists because collapsing "deployed and
answering, but the log is full of connection retries" into either success or
failure would be a lie in both directions: it is not a clean deployment, and it
is not a failed one either.

Every check carries its evidence. A verification that says "healthy" without
showing the status code it observed is an assertion, not a measurement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

VerificationStatus = Literal["SUCCESS", "WARNING", "FAILED"]

#: What a single check concluded.
#:
#: ``not_applicable`` exists because ``skipped`` cannot carry both meanings. A
#: check skipped because a prerequisite was missing is a genuine gap and must
#: surface; a check that simply does not apply to this deployment (no declared
#: dependencies) is not a concern. Conflating them would make ``WARNING`` fire on
#: every clean deployment, and a warning that always fires is one nobody reads.
CheckOutcome = Literal["pass", "warn", "fail", "skipped", "not_applicable"]

#: The seven things a deployment is verified against, in evaluation order. The
#: order matters: later checks assume earlier ones passed, so a container that
#: does not exist must not be probed for logs.
CHECK_ORDER: tuple[str, ...] = (
    "container_exists",
    "container_running",
    "port_available",
    "health_endpoint",
    "health_status_code",
    "logs_clean",
    "dependencies_reachable",
)

CHECK_DESCRIPTIONS: dict[str, str] = {
    "container_exists": "The container exists and Docker knows its name.",
    "container_running": "The container is currently running.",
    "port_available": "The expected port is published and answers.",
    "health_endpoint": "The health endpoint responds.",
    "health_status_code": "The health endpoint returns the expected HTTP status.",
    "logs_clean": "Application logs contain no obvious fatal errors.",
    "dependencies_reachable": "Expected backing services are reachable where testable.",
}

#: Log lines matching any of these are treated as evidence of a real problem.
#: Deliberately specific: a generic "error" substring would flag a log line that
#: merely mentions the word, and a verification that cries wolf gets ignored.
FATAL_LOG_PATTERNS: tuple[str, ...] = (
    r"\bpanic\b",
    r"\btraceback \(most recent call last\)",
    r"\bunhandled exception\b",
    r"\bfatal\b",
    r"\bsegmentation fault\b",
    r"\bout of memory\b",
    r"\boom-killed\b",
    r"\berrno=104\b.*connection reset",
    r"\bcannot connect to (mysql|postgres|redis|mongodb)\b",
    r"\bconnection refused\b.{0,40}\b(port|5432|6379|27017)\b",
    # Go and Node print symbolic errno names, which the prose patterns above
    # never match. "dial tcp 127.0.0.1:5432: connect: connection refused" is a
    # dead database, not a log line to shrug at.
    r"\bECONNREFUSED\b",
    r"\bECONNRESET\b",
    r"\bEADDRINUSE\b",
    r"\bENOTFOUND\b.{0,40}\b(getaddrinfo|host)\b",
    r"\bdial tcp\b.{0,60}\bconnection refused\b",
    r"\bunable to connect\b",
    r"\baddress already in use\b",
)

#: Patterns that look alarming but are routinely benign. Checked *before* the
#: fatal patterns so a startup line such as "connection pool retrying" does not
#: become a failure.
BENIGN_LOG_PATTERNS: tuple[str, ...] = (
    r"\berror[_ ]?(code|message)?\s*[:=]\s*(0|none|null)\b",
    r"\bno errors\b",
    r"\berrors? (were )?logged: 0\b",
)


class Evidence(BaseModel):
    """An observation that supports a check's outcome.

    ``source`` names the tool or field the observation came from, so a reviewer
    can tell a measurement from an inference. ``excerpt`` is capped by the caller;
    this model does not truncate, it only refuses to grow without bound at the API
    edge.
    """

    source: str = Field(description="Tool or field the observation came from.")
    detail: str = ""
    excerpt: str | None = Field(default=None, description="Short verbatim evidence.")
    value: str | None = Field(default=None, description="The observed value, if scalar.")

    def summary(self) -> str:
        """One line for the API response and logs."""
        parts = [self.source]
        if self.value is not None:
            parts.append(f"value={self.value}")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


class VerificationCheck(BaseModel):
    """One of the seven checks, and what it found."""

    name: str = Field(description="Check identifier; one of CHECK_ORDER.")
    outcome: CheckOutcome
    detail: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def counts_against_success(self) -> bool:
        """True when this check should prevent a clean SUCCESS verdict.

        A warning or a check skipped for a missing prerequisite counts; a check
        that simply does not apply does not.
        """
        return self.outcome in {"warn", "skipped", "fail"}

    @property
    def is_terminal(self) -> bool:
        """True when this check failed in a way that stops the rest.

        A missing container makes "is it running?" and "are the logs clean?"
        unanswerable rather than failed. Reporting them as failures would bury
        the one fact that matters.
        """
        return self.name == "container_exists" and self.outcome == "fail"


class VerificationResult(BaseModel):
    """The verdict on a deployment."""

    status: VerificationStatus
    checks: list[VerificationCheck] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    recommendation: str = ""
    container: str | None = None
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def passed(self) -> bool:
        """True only for a clean SUCCESS. WARNING is not a pass."""
        return self.status == "SUCCESS"

    def check(self, name: str) -> VerificationCheck | None:
        """Look up one check by name."""
        return next((item for item in self.checks if item.name == name), None)

    def outcome_of(self, name: str) -> CheckOutcome | None:
        """The outcome of one check, or ``None`` if it was not evaluated."""
        found = self.check(name)
        return found.outcome if found else None

    def summary_markdown(self) -> str:
        """Render the verdict for a human."""
        icons = {
            "pass": "✅",
            "warn": "⚠️",
            "fail": "❌",
            "skipped": "⏭️",
            "not_applicable": "➖",
        }
        lines = [
            f"# Verification: {self.status}",
            "",
            f"**Container:** `{self.container or 'unknown'}`  ",
            f"**Checks:** {sum(1 for c in self.checks if c.outcome == 'pass')}"
            f"/{len(self.checks)} passed  ",
            f"**Duration:** {self.duration_seconds:.2f}s",
            "",
            "| Check | Outcome | Detail |",
            "| --- | --- | --- |",
        ]
        for item in self.checks:
            lines.append(
                f"| `{item.name}` | {icons[item.outcome]} {item.outcome} "
                f"| {item.skipped_reason or item.detail} |"
            )
        lines.append("")

        if self.failures:
            lines += ["## Failures", ""]
            lines += [f"- {item}" for item in self.failures]
            lines.append("")
        if self.warnings:
            lines += ["## Warnings", ""]
            lines += [f"- {item}" for item in self.warnings]
            lines.append("")
        if self.evidence:
            lines += ["## Evidence", ""]
            lines += [
                f"- `{item.source}`: {item.detail or item.value or ''}" for item in self.evidence
            ]
            lines.append("")

        lines += [f"**Recommendation:** {self.recommendation}"]
        return "\n".join(lines)


class VerificationRequest(BaseModel):
    """Body of the verification endpoint and of the VERIFY stage's input."""

    container_name: str = Field(min_length=1, max_length=128)
    expected_port: int | None = Field(
        default=None, ge=1, le=65535, description="Host port the deployment published."
    )
    health_path: str = Field(
        default="/health",
        max_length=255,
        description="Path to probe on the published port.",
    )
    expected_status: int = Field(
        default=200,
        ge=100,
        le=599,
        description="HTTP status the health endpoint must return.",
    )
    log_tail: int = Field(default=200, ge=1, le=5000)
    dependencies: list[str] = Field(
        default_factory=list,
        description="Backing service names to check where a check is possible.",
    )
    include_logs: bool = Field(default=True, description="Scan application logs for fatal errors.")


class VerificationState(TypedDict, total=False):
    """LangGraph state for the VERIFY stage."""

    container_name: str
    expected_port: int | None
    health_path: str
    expected_status: int
    log_tail: int
    dependencies: list[str]
    include_logs: bool

    checks: list[VerificationCheck]
    evidence: list[Evidence]
    failures: list[str]
    warnings: list[str]
    status: VerificationStatus
    recommendation: str
    result: VerificationResult
