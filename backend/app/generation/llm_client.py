"""OpenAI / Anthropic chat-completion client for NJDOT generation.

Wraps either ``openai.OpenAI.chat.completions.create`` or
``anthropic.Anthropic.messages.create`` behind a single ``complete()`` call.
The active provider is selected by ``config.LLM_PROVIDER``:

    LLM_PROVIDER=openai      → gpt-4o  (default)
    LLM_PROVIDER=anthropic   → claude-sonnet-5

Usage
-----
    from app.generation.llm_client import LLMClient

    client = LLMClient()
    text = client.complete(system_prompt="You are …", user_message="…")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Union

import openai
import anthropic as anthropic_sdk

# ── Package-root import shim ──────────────────────────────────────────────────
try:
    from app.config import config
except ImportError:
    _pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from app.config import config  # type: ignore[no-redef]


_DEFAULT_MODEL_OPENAI    = "gpt-4o"
_DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-5"
# Sonnet 5 runs adaptive thinking by default; max_tokens must cover thinking
# plus the visible answer.
_ANTHROPIC_MAX_TOKENS    = 8192


class LLMClient:
    """Thin wrapper around OpenAI or Anthropic chat completions.

    Provider is selected by ``config.LLM_PROVIDER`` ("openai" | "anthropic").
    The ``complete()`` signature is identical regardless of provider.

    Parameters
    ----------
    api_key : str or None
        API key for the active provider.  ``None`` → read from config.
    model : str or None
        Model name override.  ``None`` → provider default.
    oai_client : openai.OpenAI or None
        Pre-built OpenAI client to reuse (ignored when provider is anthropic).
    """

    def __init__(
        self,
        api_key:    Optional[str]           = None,
        model:      Optional[str]           = None,
        oai_client: Optional[openai.OpenAI] = None,
    ) -> None:
        provider = getattr(config, "LLM_PROVIDER", "openai").lower()

        if provider == "anthropic":
            self._provider = "anthropic"
            _key = api_key or config.ANTHROPIC_API_KEY
            if not _key:
                raise ValueError(
                    "Anthropic API key is required. "
                    "Set ANTHROPIC_API_KEY in .env or pass api_key= to LLMClient."
                )
            self._client: Union[openai.OpenAI, anthropic_sdk.Anthropic] = (
                anthropic_sdk.Anthropic(api_key=_key)
            )
            self._model = model or _DEFAULT_MODEL_ANTHROPIC

        else:  # default: openai
            self._provider = "openai"
            if oai_client is not None:
                self._client = oai_client
            else:
                _key = api_key or config.OPENAI_API_KEY
                if not _key:
                    raise ValueError(
                        "OpenAI API key is required. "
                        "Set OPENAI_API_KEY in .env or pass api_key= to LLMClient."
                    )
                self._client = openai.OpenAI(api_key=_key)
            self._model = model or getattr(config, "CHAT_MODEL", _DEFAULT_MODEL_OPENAI)

    # ── Public ────────────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        """The chat model name in use."""
        return self._model

    @property
    def provider(self) -> str:
        """The active LLM provider ("openai" or "anthropic")."""
        return self._provider

    @property
    def client(self) -> Union[openai.OpenAI, anthropic_sdk.Anthropic]:
        """The underlying SDK client (used by the tool-use loop)."""
        return self._client

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Run a single-turn chat completion and return the raw response text.

        Parameters
        ----------
        system_prompt : str
            System-role instructions handed to the model.
        user_message : str
            The user turn: assembled context chunks + question.

        Returns
        -------
        str
            Raw text from the model response.

        Raises
        ------
        RuntimeError
            Wraps any provider API error with a descriptive prefix.
        """
        if self._provider == "anthropic":
            try:
                # No temperature: Sonnet 5 rejects non-default sampling params.
                message = self._client.messages.create(  # type: ignore[union-attr]
                    model=self._model,
                    max_tokens=_ANTHROPIC_MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                # Adaptive thinking may prepend thinking blocks — take text blocks.
                return "\n".join(
                    b.text for b in message.content if b.type == "text"
                ).strip()
            except Exception as exc:
                raise RuntimeError(
                    f"LLM completion failed [{type(exc).__name__}]: {exc}"
                ) from exc

        else:  # openai
            try:
                response = self._client.chat.completions.create(  # type: ignore[union-attr]
                    model=self._model,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                )
                return response.choices[0].message.content or ""
            except openai.OpenAIError as exc:
                raise RuntimeError(
                    f"LLM completion failed [{type(exc).__name__}]: {exc}"
                ) from exc
