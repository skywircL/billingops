"""The language model seam.

The graph never imports a provider SDK. It takes a callable, and everything
else in this package is written against that callable. Tests inject a
deterministic stand-in, the evaluation harness measures the stand-in, and
production passes an adapter over a real chat model. Nothing changes shape
in between.
"""

from .base import LLMCallable, LLMRequest
from .cassette import Cassette, CassetteMiss, recording, replaying
from .chat_model import ChatModelLike, from_chat_model
from .offline import MalformedLLM, OfflineSupportLLM, ScriptedLLM

__all__ = [
    "Cassette",
    "CassetteMiss",
    "ChatModelLike",
    "LLMCallable",
    "LLMRequest",
    "MalformedLLM",
    "OfflineSupportLLM",
    "ScriptedLLM",
    "from_chat_model",
    "recording",
    "replaying",
]
