"""Unit tests for council LLM-call robustness (streaming, retry, big batches)."""

import unittest
from unittest.mock import patch

import pytest

from tradingagents.allocation import council as c


class _Chunk:
    def __init__(self, content):
        self.content = content


class _Msg:
    def __init__(self, content):
        self.content = content


class _StreamingLLM:
    """Streams three chunks; invoke should not be needed."""
    def __init__(self):
        self.invoked = 0

    def stream(self, messages):
        yield from (_Chunk("Hello "), _Chunk("council "), _Chunk("world"))

    def invoke(self, messages):
        self.invoked += 1
        return _Msg("fallback")


class _NoStreamLLM:
    """No usable stream() — must fall back to invoke."""
    def stream(self, messages):
        raise NotImplementedError

    def invoke(self, messages):
        return _Msg("invoked directly")


class _BlockStreamLLM:
    """Streams anthropic-style typed content blocks."""
    def stream(self, messages):
        yield _Chunk([{"type": "text", "text": "block "}, {"type": "text", "text": "content"}])

    def invoke(self, messages):
        return _Msg("fallback")


class _FlakyLLM:
    """Fails transiently N times before succeeding."""
    def __init__(self, failures, exc_cls=ConnectionError):
        self.failures = failures
        self.exc_cls = exc_cls
        self.calls = 0

    def stream(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc_cls("Connection error.")
        yield _Chunk("recovered")

    def invoke(self, messages):
        return _Msg("recovered")


class BadRequestError(Exception):
    pass


@pytest.mark.unit
class CallStreamingTests(unittest.TestCase):
    def test_streamed_chunks_are_joined(self):
        llm = _StreamingLLM()
        self.assertEqual(c._call(llm, "s", "h"), "Hello council world")
        self.assertEqual(llm.invoked, 0)

    def test_falls_back_to_invoke_without_stream(self):
        self.assertEqual(c._call(_NoStreamLLM(), "s", "h"), "invoked directly")

    def test_typed_block_chunks_are_joined(self):
        self.assertEqual(c._call(_BlockStreamLLM(), "s", "h"), "block content")


@pytest.mark.unit
class CallRetryTests(unittest.TestCase):
    def test_transient_failure_is_retried(self):
        llm = _FlakyLLM(failures=2)
        with patch.object(c.time, "sleep") as sleep:
            self.assertEqual(c._call(llm, "s", "h"), "recovered")
        self.assertEqual(llm.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_exhausted_retries_raise_last_error(self):
        llm = _FlakyLLM(failures=99)
        with patch.object(c.time, "sleep"):
            with self.assertRaises(ConnectionError):
                c._call(llm, "s", "h")
        self.assertEqual(llm.calls, c._CALL_ATTEMPTS)

    def test_non_retryable_error_raises_immediately(self):
        llm = _FlakyLLM(failures=99, exc_cls=BadRequestError)
        with patch.object(c.time, "sleep") as sleep:
            with self.assertRaises(BadRequestError):
                c._call(llm, "s", "h")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(sleep.call_count, 0)


@pytest.mark.unit
class CondensedSectionsTests(unittest.TestCase):
    def _ctx(self, ticker):
        return {"ticker": ticker, "pm_decision": "FULL PM TEXT",
                "brief_summary": "FULL BRIEF TEXT"}

    def test_full_sections_include_reports(self):
        out = c._format_sections([self._ctx("AAA")])
        self.assertIn("FULL PM TEXT", out)
        self.assertIn("FULL BRIEF TEXT", out)

    def test_condensed_sections_omit_reports(self):
        out = c._format_sections([self._ctx("AAA")], include_reports=False)
        self.assertNotIn("FULL PM TEXT", out)
        self.assertNotIn("FULL BRIEF TEXT", out)
        self.assertIn("omitted in large batch", out)
        self.assertIn("AAA", out)  # data lines stay


if __name__ == "__main__":
    unittest.main()
