"""Bound and redact free-text inputs before using the shared model helper."""

from collections import OrderedDict
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, StringConstraints, field_validator

from alerting import llm


class TextInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=llm.MAX_INPUT_CHARS)]

    @field_validator("text")
    @classmethod
    def valid_text(cls, text):
        if "\0" in text:
            raise ValueError("NUL characters are not supported")
        text.encode("utf-8")
        return text


_cache: OrderedDict[tuple, BaseModel] = OrderedDict()


async def text_hints(model, *, system, text):
    if not llm.enabled():
        return None
    safe_bytes = llm.redact(text).encode("utf-8")[:llm.MAX_INPUT_CHARS]
    key = (model, llm.MODEL, system, sha256(safe_bytes).digest())
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    result = await llm.structured(model, system=system, user=safe_bytes.decode("utf-8", errors="ignore"))
    if result is not None:
        _cache[key] = result
        if len(_cache) > 128:
            _cache.popitem(last=False)
    return result
