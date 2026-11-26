"""Minimal JSON-RPC transport for log acquisition.

:class:`RpcClient` is the protocol the sync engine depends on; tests substitute an
in-memory fake. :class:`HttpRpcClient` is the production implementation, tuned for hosted
endpoints where some requests hang: a short per-request timeout, immediate retry on
timeout or connection failure, and one keep-alive HTTPS session per thread (the sync
engine fetches block ranges from a thread pool).

A node that rejects a ``eth_getLogs`` because the range or result set is too large raises
:class:`RangeTooLargeError`, which the sync engine answers by splitting the range.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Protocol

import requests


class RpcError(RuntimeError):
    """The node returned a JSON-RPC error, or retries were exhausted."""


class RangeTooLargeError(RpcError):
    """``eth_getLogs`` was refused because the block range or result was too large."""


#: Lowercased substrings of node error messages that mean "narrow the range and retry".
_RANGE_ERROR_HINTS = (
    "range",
    "too many",
    "limit exceeded",
    "query returned more than",
    "response size",
    "max results",
    "blocks without the log index",
)


class RpcClient(Protocol):
    """The eth JSON-RPC surface the sync engine needs."""

    def finalized_block(self) -> int:
        """Number of the chain's ``finalized`` block."""
        ...

    def get_logs(
        self, *, address: str, topics: list[Any] | None, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        """Raw logs for ``[from_block, to_block]`` (inclusive) emitted by ``address``."""
        ...


class HttpRpcClient:
    """:class:`RpcClient` over HTTP(S) JSON-RPC.

    Args:
        url: Endpoint URL.
        timeout: Per-request timeout in seconds. Kept short; a hung request is abandoned
            and retried immediately rather than waited on.
        max_attempts: Attempts per call before giving up with :class:`RpcError`.
        split_after_timeouts: Consecutive timeouts after which a multi-block
            ``eth_getLogs`` is reported as :class:`RangeTooLargeError`, so the sync engine
            bisects it. Some nodes are consistently slower than ``timeout`` on dense or
            uncached ranges; retrying the same range forever would never succeed.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 3.0,
        max_attempts: int = 8,
        split_after_timeouts: int = 3,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.split_after_timeouts = split_after_timeouts
        self._local = threading.local()
        self._ids = itertools.count(1)

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["Content-Type"] = "application/json"
            self._local.session = session
        return session

    def call(self, method: str, params: list[Any], *, splittable: bool = False) -> Any:
        """One JSON-RPC call with retry.

        Timeouts and connection errors are retried immediately; HTTP 429/5xx and
        non-range JSON-RPC errors are retried with a short linear backoff. A range error
        is raised at once as :class:`RangeTooLargeError`, and so are
        ``split_after_timeouts`` timeouts in a row when ``splittable``.
        """
        last: Exception | None = None
        timeouts = 0
        for attempt in range(self.max_attempts):
            body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            try:
                resp = self._session().post(self.url, json=body, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
                self._local.session = None  # drop a possibly wedged connection
                if isinstance(exc, requests.Timeout):
                    timeouts += 1
                    if splittable and timeouts >= self.split_after_timeouts:
                        raise RangeTooLargeError(f"{method} timed out {timeouts} times") from exc
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = RpcError(f"HTTP {resp.status_code}")
                time.sleep(0.5 * (attempt + 1))
                continue
            try:
                payload = resp.json()
            except ValueError as exc:
                last = RpcError(f"non-JSON response (HTTP {resp.status_code})")
                last.__cause__ = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            err = payload.get("error")
            if err is None:
                return payload["result"]
            message = str(err.get("message", err))
            if any(h in message.lower() for h in _RANGE_ERROR_HINTS):
                raise RangeTooLargeError(message)
            last = RpcError(f"{method}: {message}")
            time.sleep(0.5 * (attempt + 1))
        raise RpcError(f"{method} failed after {self.max_attempts} attempts: {last}")

    def finalized_block(self) -> int:
        return int(self.call("eth_getBlockByNumber", ["finalized", False])["number"], 16)

    def get_logs(
        self, *, address: str, topics: list[Any] | None, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        flt: dict[str, Any] = {
            "address": address,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if topics is not None:
            flt["topics"] = topics
        return self.call("eth_getLogs", [flt], splittable=to_block > from_block)
