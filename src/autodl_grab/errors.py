class Pause(Exception):
    """Human review is required; never fall through to another paid operation."""


class NoResource(Exception):
    """Only an explicit, non-committing inventory rejection."""


class CloneExhausted(Exception):
    pass


class Retryable(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


TRANSIENT_PAGE_CODES = frozenset({
    "host_selection_pending", "host_table_loading", "duplicate_host_rows",
    "disk_fee_pending", "hourly_fee_pending", "quote_pending",
    "quote_transport_pending", "quote_selection_pending", "models_pending",
    "host_rows_pending", "console_render_pending",
    "quote_changed", "quote_expired",
})


class TransientPage(Pause):
    """A known pre-click page wait; callers may rebuild the entire form."""


class CandidateRetry(Retryable):
    """Pre-submit form retries exhausted; cool down only this candidate."""


class QuoteOverBudget(Pause):
    """A valid quote exceeds the budget; never submit this candidate."""


class InstanceNotVisible(Pause):
    """A known instance is not in the current API snapshot; only a pending action may wait."""


def browser_transient(error):
    """Classify transport/lifecycle failures without treating guard refusals as transient."""
    if isinstance(error, TimeoutError):
        return True
    if not type(error).__module__.startswith("playwright."):
        return False
    if type(error).__name__ in {"TimeoutError", "TargetClosedError"}:
        return True
    from .diagnostics import NETWORK_CODES
    import re
    return bool(set(re.findall(r"\bnet::(ERR_[A-Z0-9_]+)\b", str(error))) & NETWORK_CODES)


class Ambiguous(Pause):
    pass


class NotSubmitted(Pause):
    """The page guard explicitly proved that no final click occurred."""
