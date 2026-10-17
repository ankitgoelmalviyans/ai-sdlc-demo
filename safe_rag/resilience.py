"""Self-healing for the model call: retry, circuit breaker, fallback.

A RAG service should degrade, not die, when the model provider has a bad
minute. The chain is:

    primary LLM --(retry with backoff)--> still failing?
        --> circuit opens (stop hammering it for `reset_after` seconds)
        --> fallback LLM (here: the extractive stub, which can't fail)
"""

import time
from collections.abc import Callable


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """Opens after `threshold` consecutive failures, half-opens after `reset_after` seconds."""

    def __init__(self, threshold: int = 3, reset_after: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.reset_after = reset_after
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_after:
            return "half_open"
        return "open"

    def call(self, fn: Callable[[], str]) -> str:
        if self.state == "open":
            raise CircuitOpen()
        try:
            result = fn()
        except Exception:
            self.failures += 1
            if self.failures >= self.threshold or self.state == "half_open":
                self.opened_at = self.clock()
            raise
        self.failures, self.opened_at = 0, None
        return result


def retry(fn: Callable[[], str], attempts: int = 3, base_delay: float = 0.2,
          sleep: Callable[[float], None] = time.sleep) -> str:
    """Exponential backoff: base, 2*base, 4*base ... Re-raises the last error."""
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            sleep(base_delay * 2 ** i)
    raise AssertionError("unreachable")


class ResilientLLM:
    """Wraps a primary LLM with retry + circuit breaker, falling back on failure.

    `events` records what healing happened so the pipeline can audit it.
    """

    def __init__(self, primary, fallback, breaker: CircuitBreaker | None = None,
                 attempts: int = 3, sleep: Callable[[float], None] = time.sleep):
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker or CircuitBreaker()
        self.attempts = attempts
        self.sleep = sleep
        self.events: list[str] = []

    def complete(self, system: str, context: str, question: str) -> str:
        self.events = []
        try:
            return self.breaker.call(lambda: retry(
                lambda: self.primary.complete(system, context, question),
                attempts=self.attempts, sleep=self.sleep))
        except CircuitOpen:
            self.events.append("llm_circuit_open")
        except Exception as e:
            self.events.append(f"llm_failed:{type(e).__name__}")
        self.events.append("llm_fallback")
        return self.fallback.complete(system, context, question)
