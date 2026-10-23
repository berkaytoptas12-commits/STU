"""One question being answered: it can be stopped, and it records where its time goes.

The context travels with the work in a context variable: the thread that answers the question sets it, and work it
hands to a thread pool takes it along (``submit``). Model calls look it up: a stopped question closes its open
request to the model server at once - the server then stops generating for it - and raises ``Cancelled``. Every
model call also records how long it took, how long the first token took and how many tokens it produced, under
the stage it belongs to (planning, answer, judge, ...), so an answer can show where its time went.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Callable, Iterator, Optional


class Cancelled(Exception):
    """The user stopped the question."""


@dataclass
class CallStat:
    stage: str
    seconds: float
    first_token: Optional[float]      # seconds until the first streamed token (queue + prompt processing)
    completion_tokens: int            # all generated tokens, thinking included (server count, or estimated)
    reasoning_tokens: int             # of those, thinking (estimated from the text when the server does not say)
    prompt_tokens: int = 0
    cached_tokens: int = 0            # prompt tokens the server took from its prefix cache (when it reports it)
    estimated: bool = False           # token counts estimated from text length (the server sent no usage)
    ok: bool = True


class AskContext:
    def __init__(self):
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._closers: list[Callable[[], None]] = []
        self.calls: list[CallStat] = []
        self.started = time.time()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        """Stop the question: open model requests are closed now; the work ends at its next check."""
        self._stopped.set()
        with self._lock:
            closers = list(self._closers)
        for close in closers:
            try:
                close()
            except Exception:
                pass

    def check(self) -> None:
        if self.stopped:
            raise Cancelled()

    @contextmanager
    def closing(self, close: Callable[[], None]) -> Iterator[None]:
        """While the block runs, stop() also calls close() (e.g. closes a streaming HTTP response)."""
        with self._lock:
            self._closers.append(close)
        try:
            if self.stopped:
                close()
                raise Cancelled()
            yield
        finally:
            with self._lock:
                if close in self._closers:
                    self._closers.remove(close)

    def record(self, stat: CallStat) -> None:
        with self._lock:
            self.calls.append(stat)

    def summary(self) -> dict:
        """Model calls per stage: count, time, slowest first token, tokens and generation speed."""
        with self._lock:
            calls = list(self.calls)
        out: dict = {}
        for c in calls:
            s = out.setdefault(c.stage, {"calls": 0, "seconds": 0.0, "first_token_max": 0.0, "completion_tokens": 0,
                                         "reasoning_tokens": 0, "prompt_tokens": 0, "cached_tokens": 0,
                                         "estimated": False, "failed": 0, "_gen": 0.0})
            s["calls"] += 1
            s["seconds"] += c.seconds
            s["first_token_max"] = max(s["first_token_max"], c.first_token or 0.0)
            s["completion_tokens"] += c.completion_tokens
            s["reasoning_tokens"] += c.reasoning_tokens
            s["prompt_tokens"] += c.prompt_tokens
            s["cached_tokens"] += c.cached_tokens
            s["estimated"] = s["estimated"] or c.estimated
            s["failed"] += 0 if c.ok else 1
            s["_gen"] += max(0.0, c.seconds - (c.first_token or 0.0))
        for s in out.values():
            gen = s.pop("_gen")
            measurable = gen > 0.05 and s["completion_tokens"]
            s["tokens_per_second"] = round(s["completion_tokens"] / gen, 1) if measurable else None
            s["seconds"] = round(s["seconds"], 2)
            s["first_token_max"] = round(s["first_token_max"], 2)
        return out

    def calls_dicts(self) -> list[dict]:
        with self._lock:
            return [asdict(c) for c in self.calls]


_CURRENT: contextvars.ContextVar[Optional[AskContext]] = contextvars.ContextVar("techrag_ask", default=None)
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("techrag_stage", default="other")


def current() -> Optional[AskContext]:
    return _CURRENT.get()


def check() -> None:
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.check()


def stage_name() -> str:
    return _STAGE.get()


@contextmanager
def active(ctx: Optional[AskContext]) -> Iterator[Optional[AskContext]]:
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    # restored by value, not with the token: a generator that yields inside the block may be resumed in another
    # context, where resetting the token would raise
    previous = _STAGE.get()
    _STAGE.set(name)
    try:
        yield
    finally:
        _STAGE.set(previous)


def submit(pool, fn, *args, **kwargs):
    """pool.submit that carries the question (and its stage) into the worker thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)
