"""Error classification, retry backoff, and the adaptive CDN throttle."""

from __future__ import annotations

import asyncio

import pytest

from app.core.downloader import (
    AdaptiveLimiter,
    _backoff_delay,
    classify_exception,
    classify_http_status,
)
from app.core.models import ErrorClass


class TestErrorClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (403, ErrorClass.HTTP_403),
            (404, ErrorClass.HTTP_404),
            (429, ErrorClass.HTTP_429),
            (500, ErrorClass.HTTP_5XX),
            (503, ErrorClass.HTTP_5XX),
        ],
    )
    def test_status_mapping(self, status, expected):
        assert classify_http_status(status) is expected

    @pytest.mark.parametrize(
        "cls,retryable",
        [
            (ErrorClass.HTTP_429, True),
            (ErrorClass.HTTP_5XX, True),
            (ErrorClass.TIMEOUT, True),
            (ErrorClass.DNS_ERROR, True),
            (ErrorClass.OCR_ERROR, True),
            # A dead signed URL and a 404 will never come back — retrying them
            # just burns time and CDN goodwill.
            (ErrorClass.EXPIRED_URL, False),
            (ErrorClass.HTTP_404, False),
            (ErrorClass.HTTP_403, False),
            (ErrorClass.DECODE_ERROR, False),
            (ErrorClass.TOO_LARGE, False),
        ],
    )
    def test_retryability(self, cls, retryable):
        assert cls.retryable is retryable

    def test_asyncio_timeout_classified(self):
        cls, detail = classify_exception(asyncio.TimeoutError())
        assert cls is ErrorClass.TIMEOUT
        assert "TimeoutError" in detail

    def test_unknown_exception_is_retryable_not_fatal(self):
        cls, _ = classify_exception(RuntimeError("who knows"))
        assert cls.retryable is True


class TestBackoff:
    def test_grows_with_attempt(self):
        assert min(_backoff_delay(1) for _ in range(50)) < max(
            _backoff_delay(3) for _ in range(50)
        )

    def test_jitter_spreads_retries(self):
        # Without jitter a whole batch retries in lockstep and re-trips the limit.
        assert len({round(_backoff_delay(3), 4) for _ in range(50)}) > 10

    def test_cap_holds_after_jitter(self):
        # The cap must be applied after the multiplier, not before.
        assert max(_backoff_delay(a) for a in range(1, 12) for _ in range(200)) <= 30.0

    def test_never_negative(self):
        assert min(_backoff_delay(a) for a in range(1, 12) for _ in range(100)) > 0


class TestAdaptiveLimiter:
    @pytest.mark.asyncio
    async def test_starts_at_configured_concurrency(self):
        assert AdaptiveLimiter(16, 2).limit == 16

    @pytest.mark.asyncio
    async def test_single_throttle_does_not_react(self):
        # One stray 429 is noise, not a signal.
        limiter = AdaptiveLimiter(16, 2)
        await limiter.record_throttled()
        assert limiter.limit == 16

    @pytest.mark.asyncio
    async def test_sustained_throttling_halves_concurrency(self):
        limiter = AdaptiveLimiter(16, 2)
        for _ in range(3):
            await limiter.record_throttled()
        assert limiter.limit == 8

    @pytest.mark.asyncio
    async def test_never_drops_below_the_floor(self):
        limiter = AdaptiveLimiter(16, 2)
        for _ in range(60):
            await limiter.record_throttled()
        assert limiter.limit == 2

    @pytest.mark.asyncio
    async def test_recovers_after_a_clean_streak(self):
        limiter = AdaptiveLimiter(16, 2)
        for _ in range(3):
            await limiter.record_throttled()
        for _ in range(200):
            await limiter.record_ok()
        assert limiter.limit == 9

    @pytest.mark.asyncio
    async def test_acts_as_a_semaphore(self):
        limiter = AdaptiveLimiter(2, 1)
        peak = 0
        current = 0

        async def worker():
            nonlocal peak, current
            async with limiter:
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.01)
                current -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak <= 2
