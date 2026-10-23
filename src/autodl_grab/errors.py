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


class Ambiguous(Pause):
    pass
