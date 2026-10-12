"""Record real model calls once, replay them forever.

The only reason a ``--live`` path exists in this repo is to fill a cassette.
Once a cassette is on disk, the same run is reproducible with no key and no
spend, and a reviewer can read exactly what the model said.

Replay is strict by default: a request that is not in the cassette raises
:class:`CassetteMiss` instead of quietly calling out. A test that starts
costing money is a test that stopped being a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, MutableMapping

from .base import LLMCallable, LLMRequest


class CassetteMiss(KeyError):
    """A request was replayed that the cassette has never seen."""


class Cassette:
    """A JSON file of request key to response text."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: MutableMapping[str, dict[str, str]] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __contains__(self, request: object) -> bool:
        return isinstance(request, LLMRequest) and request.key() in self._entries

    def get(self, request: LLMRequest) -> str | None:
        entry = self._entries.get(request.key())
        return entry["response"] if entry else None

    def put(self, request: LLMRequest, response: str) -> None:
        self._entries[request.key()] = {
            "tag": request.tag,
            "system": request.system,
            "user": request.user,
            "response": response,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._entries, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def recording(inner: LLMCallable, cassette: Cassette, *, reuse: bool = True) -> LLMCallable:
    """Call ``inner`` and write every response to the cassette.

    Args:
        inner: the real model callable.
        cassette: destination, saved after each call so a crash keeps what it got.
        reuse: when True, an already recorded request is served from the
            cassette instead of being paid for twice.
    """

    def call(request: LLMRequest) -> str:
        if reuse:
            cached = cassette.get(request)
            if cached is not None:
                return cached
        response = inner(request)
        cassette.put(request, response)
        cassette.save()
        return response

    return call


def replaying(cassette: Cassette, *, fallback: LLMCallable | None = None) -> LLMCallable:
    """Serve responses from the cassette.

    Args:
        cassette: the recorded calls.
        fallback: optional callable for misses. Leave it None to make a miss
            an error, which is what a test suite wants.
    """

    def call(request: LLMRequest) -> str:
        cached = cassette.get(request)
        if cached is not None:
            return cached
        if fallback is not None:
            return fallback(request)
        raise CassetteMiss(f"no recorded response for tag={request.tag} key={request.key()}")

    return call
