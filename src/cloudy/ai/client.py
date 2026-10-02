"""Minimal client for llama-server's OpenAI-compatible chat endpoint: stdlib only, loopback only, streaming."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import is_loopback


class ClientError(Exception):
    pass


@dataclass
class Completion:
    text: str
    chunks: int  # streamed content chunks (llama-server sends about one token per chunk)
    first_token_s: float | None
    total_s: float


class Client:
    def __init__(self, base_url: str, *, timeout: float = 300.0, seed: int = 42, max_tokens: int = 1024) -> None:
        if not is_loopback(base_url):
            raise ClientError(f"refusing non-loopback model endpoint: {base_url}")
        parsed = urlparse(base_url)
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.timeout, self.seed, self.max_tokens = timeout, seed, max_tokens

    def complete(self, messages: list[dict], schema: dict | None = None,
                 on_token: Callable[[str], None] | None = None) -> Completion:
        """One reply at temperature 0 with a fixed seed; `schema` constrains it to matching JSON."""
        body: dict = {"messages": messages, "stream": True, "temperature": 0, "seed": self.seed,
                      "max_tokens": self.max_tokens}
        if schema is not None:
            body["response_format"] = {"type": "json_object", "schema": schema}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        start, first, parts = time.monotonic(), None, []
        try:
            connection.request("POST", "/v1/chat/completions", json.dumps(body),
                               {"Content-Type": "application/json", "Accept": "text/event-stream"})
            response = connection.getresponse()
            if response.status != 200:
                raise ClientError(f"model server answered HTTP {response.status}: {response.read(500)!r}")
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content") or ""
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    first = first if first is not None else time.monotonic() - start
                    parts.append(delta)
                    if on_token:
                        on_token(delta)
        except OSError as exc:
            raise ClientError(f"model server unreachable: {exc}") from exc
        finally:
            connection.close()
        return Completion("".join(parts), len(parts), first, time.monotonic() - start)
