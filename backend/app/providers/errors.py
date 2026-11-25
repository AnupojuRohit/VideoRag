from datetime import datetime


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        retryable: bool,
        transient: bool = False,
        status_code: int | None = None,
        retry_after: datetime | None = None,
        quota: bool = False,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable
        self.transient = transient
        self.status_code = status_code
        self.retry_after = retry_after
        self.quota = quota
        self.latency_ms = latency_ms
        self.request_id = request_id


class TranscriptionProviderError(ProviderError):
    pass


class VisionProviderError(ProviderError):
    pass


class EmbeddingProviderError(ProviderError):
    pass


class GenerationProviderError(ProviderError):
    pass
