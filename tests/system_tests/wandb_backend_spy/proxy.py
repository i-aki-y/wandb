from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import traceback
from typing import Iterator

import fastapi
import httpx
import uvicorn

from .spy import WandbBackendSpy


@contextlib.contextmanager
def spy_proxy(
    proxy_port: int,
    target_host: str,
    target_port: int,
) -> Iterator[WandbBackendProxy]:
    """A context manager that proxies a W&B backend.

    The proxy server will be available at 127.0.0.1 on the given port.
    The server is shut down when the context manager exits.

    Args:
        proxy_port: The port on which to run the proxy server.
        target_host: The hostname of the W&B backend to proxy.
        target_port: The port of the W&B backend to proxy.

    Yields:
        A reference to the proxy server that can be used to create spies.
    """

    http_client = httpx.AsyncClient()
    spy = WandbBackendProxy(
        client=http_client,
        target_host=target_host,
        target_port=target_port,
    )
    server = uvicorn.Server(uvicorn.Config(spy._to_fast_api(), port=proxy_port))

    proxy_thread = threading.Thread(
        target=asyncio.run,
        args=[_serve_then_close(server, http_client)],
    )
    proxy_thread.start()
    _wait_for_server(server)

    try:
        yield spy
    finally:
        try:
            server.should_exit = True
            proxy_thread.join(timeout=30)
        except TimeoutError as e:
            raise AssertionError("Backend proxy server failed to shut down.") from e


async def _serve_then_close(
    server: uvicorn.Server,
    http_client: httpx.AsyncClient,
) -> None:
    """Start the server, wait for it to shut down, then clean up."""
    await server.serve()
    await http_client.aclose()


def _wait_for_server(server: uvicorn.Server) -> None:
    """Wait until the server has started.

    Raises:
        AssertionError: if the server does not start within a timeout.
    """
    start_time = time.monotonic()
    timeout_sec = 10

    while time.monotonic() - start_time < timeout_sec and not server.started:
        time.sleep(0.1)

    if not server.started:
        raise AssertionError("Backend proxy server failed to start.")


_RELAYABLE_REQUEST_HEADERS = {
    "authorization",
    "x-wandb-username",
    "user-agent",
    "content-type",
}
_RELAYABLE_RESPONSE_HEADERS = {"content-type"}


class WandbBackendProxy:
    """An object that can be used to insert spies into the W&B backend proxy."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        target_host: str,
        target_port: int,
    ):
        self._lock = threading.Lock()
        self._client = client
        self._target_host = target_host
        self._target_port = target_port
        self._spy: WandbBackendSpy | None = None

    @contextlib.contextmanager
    def spy(self) -> Iterator[WandbBackendSpy]:
        """A context manager for intercepting W&B requests.

        Args:
            spy: The spy which sees requests to the W&B backend and
                its responses, and which can inject fake responses.

        Raises:
            AssertionError: if there's another spy.
        """
        with self._lock:
            if self._spy:
                raise AssertionError("A spy is already attached.")
            self._spy = WandbBackendSpy()

        yield self._spy

        with self._lock:
            self._spy = None

    def _to_fast_api(self) -> fastapi.FastAPI:
        """Returns an ASGI object implemented by this spy."""
        app = fastapi.FastAPI()

        app.post("/graphql")(self._post_graphql)
        app.post(
            "/files/{entity}/{project}/{run_id}/file_stream",
        )(self._post_file_stream)

        return app

    async def _relay(self, request: fastapi.Request) -> fastapi.Response:
        """Forward the request to the actual backend and get the response."""
        forwarded_request = self._client.build_request(
            method=request.method,
            url=str(
                request.url.replace(
                    hostname=self._target_host,
                    port=self._target_port,
                )
            ),
            content=await request.body(),
        )
        for header, value in request.headers.items():
            if header in _RELAYABLE_REQUEST_HEADERS:
                forwarded_request.headers[header] = value

        response = await self._client.send(forwarded_request)

        forwarded_response = fastapi.Response(
            content=response.content,
            status_code=response.status_code,
        )
        for header, value in forwarded_response.headers.items():
            if header in _RELAYABLE_RESPONSE_HEADERS:
                forwarded_response.headers[header] = value

        return forwarded_response

    async def _post_graphql(self, request: fastapi.Request) -> fastapi.Response:
        """Handle a GraphQL request and maybe relay it to the backend."""
        with _continue_on_failure():
            body = await request.body()
            with self._lock:
                if self._spy:
                    response = self._spy.post_graphql(body)
                    if response:
                        return response

        return await self._relay(request)

    async def _post_file_stream(
        self,
        request: fastapi.Request,
        *,
        entity: str,
        project: str,
        run_id: str,
    ) -> fastapi.Response:
        """Handle a FileStream request and maybe relay it to the backend."""
        with _continue_on_failure():
            body = await request.body()
            with self._lock:
                if self._spy:
                    response = self._spy.post_file_stream(
                        body,
                        entity=entity,
                        project=project,
                        run_id=run_id,
                    )
                    if response:
                        return response

        return await self._relay(request)


@contextlib.contextmanager
def _continue_on_failure() -> Iterator[None]:
    """A context manager that prints a traceback and continues on error."""
    try:
        yield
    except Exception as e:
        traceback.print_exception(e)