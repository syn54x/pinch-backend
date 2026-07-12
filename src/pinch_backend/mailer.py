"""Outbound mail behind a minimal pluggable interface (PRD M2).

v0 ships exactly one backend: console — sufficient delivery for development
and a single-user self-host. SMTP arrives as another backend behind the
same config knob (ADR-0002: config, never forks).
"""

from abc import ABC, abstractmethod

from pinch_backend.settings import settings


class Mailer(ABC):
    @abstractmethod
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer(Mailer):
    """Prints the message to stdout — that IS the delivery.

    Deliberately ``print``, never the structured logger: mail bodies carry
    secrets (verification and reset links), which must not enter logs or
    spans. A hosted instance configures a real backend; this one existing
    at all is what makes the flows testable end to end.
    """

    async def send(self, *, to: str, subject: str, body: str) -> None:
        print(f"=== mail to {to} — {subject} ===\n{body}\n===")


def get_mailer() -> Mailer:
    if settings.mailer_backend == "console":
        return ConsoleMailer()
    raise LookupError(f"Unknown mailer backend '{settings.mailer_backend}'")
