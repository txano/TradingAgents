"""Human-readable descriptions for LLM provider exceptions.

Provider SDKs routinely collapse very different faults into one opaque message.
The openai SDK is the worst offender: its request loop ends in

    except Exception as err:
        raise APIConnectionError(request=request) from err

so a DNS failure, a TLS reset, a read timeout, a connection-pool timeout and an
``OSError: Too many open files`` all surface identically as the string
``"Connection error."``. The real fault is only reachable through the chained
``__cause__``/``__context__``.

``describe_exc`` walks that chain so logs and user-facing error lines name the
actual problem instead of the placeholder.
"""

from __future__ import annotations

# Chains are short in practice; the cap only guards against a cycle.
_MAX_DEPTH = 5


def exc_chain(exc: BaseException, max_depth: int = _MAX_DEPTH) -> list[BaseException]:
    """``exc`` plus its chained causes, outermost first, de-duplicated."""
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(out) < max_depth:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def describe_exc(exc: BaseException, max_depth: int = _MAX_DEPTH) -> str:
    """One-line description of ``exc`` including the chained root cause.

    Falls back to plain ``str(exc)`` when there is nothing extra to add, so
    callers can use it unconditionally in place of ``str(exc)``.
    """
    chain = exc_chain(exc, max_depth)
    head = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    seen_text = {str(exc)}
    parts: list[str] = []
    for cur in chain[1:]:
        text = str(cur)
        if text in seen_text:
            continue
        seen_text.add(text)
        parts.append(f"{type(cur).__name__}: {text}" if text else type(cur).__name__)
    return f"{head} <= {' <= '.join(parts)}" if parts else head
