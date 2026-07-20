"""Small dependency-free GraphQL client for Sports.ru."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://www.sports.ru/gql/graphql/"


class GraphQLRequestError(RuntimeError):
    """Raised when the remote endpoint cannot return usable GraphQL data."""


@dataclass(frozen=True)
class ClientConfig:
    endpoint: str = DEFAULT_ENDPOINT
    timeout_seconds: float = 30.0
    attempts: int = 3
    user_agent: str = "fantasy-analytics-discovery/0.1"


Transport = Callable[[Request, float], bytes]


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class SportsGraphQLClient:
    """Execute public read-only operations against the Sports.ru endpoint."""

    def __init__(
        self,
        config: ClientConfig | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.config = config or ClientConfig()
        self._transport = transport or _default_transport

    def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(
            {"query": query, "variables": dict(variables or {})},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self.config.user_agent,
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(1, self.config.attempts + 1):
            try:
                payload = json.loads(
                    self._transport(request, self.config.timeout_seconds)
                )
                if not isinstance(payload, dict):
                    raise GraphQLRequestError(
                        "Sports.ru returned a non-object JSON response"
                    )
                if payload.get("errors"):
                    raise GraphQLRequestError(
                        f"Sports.ru returned GraphQL errors: {payload['errors']}"
                    )
                if "data" not in payload:
                    raise GraphQLRequestError(
                        "Sports.ru response does not contain data"
                    )
                return payload
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt < self.config.attempts:
                    time.sleep(2 ** (attempt - 1))

        raise GraphQLRequestError(
            f"Sports.ru request failed after {self.config.attempts} attempts"
        ) from last_error
