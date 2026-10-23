from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opsteam_api.main import MAX_BODY_BYTES, create_app
from opsteam_core.errors import InvalidId
from opsteam_db import create_engine
from pydantic import BaseModel

PROBLEM = "application/problem+json"


class Body(BaseModel):
    password: str
    count: int


def _app() -> FastAPI:
    # Port 1 refuses connections, so readiness fails without a database.
    app = create_app(engine=create_engine("postgresql+asyncpg://x:y@127.0.0.1:1/none"))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret detail from /internal/path.py")

    @app.get("/bad-id")
    async def bad_id() -> None:
        raise InvalidId("not an id")

    @app.post("/echo")
    async def echo(body: Body) -> dict[str, int]:
        return {"count": body.count}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_app(), raise_server_exceptions=False) as c:
        yield c


def _assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status
    assert resp.headers["content-type"] == PROBLEM
    body: dict[str, Any] = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["request_id"] == resp.headers["x-request-id"]
    return body


def test_healthz_and_request_id(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(resp.headers["x-request-id"]) == 26


def test_request_id_is_not_taken_from_client(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-Id": "attacker-chosen"})
    assert resp.headers["x-request-id"] != "attacker-chosen"


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    _assert_problem(client.get("/api/v1/nope"), 404, "http.not_found")


def test_unexpected_error_hides_details(client: TestClient) -> None:
    resp = client.get("/boom")
    body = _assert_problem(resp, 500, "internal")
    assert "secret" not in resp.text and "RuntimeError" not in resp.text
    assert "detail" not in body or body["detail"] == "Internal error"


def test_opsteam_error_maps_to_status(client: TestClient) -> None:
    _assert_problem(client.get("/bad-id"), 422, "id.invalid")


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    resp = client.post("/echo", json={"password": "hunter2-fake", "count": "nan-ish"})
    body = _assert_problem(resp, 422, "request.invalid")
    assert "hunter2-fake" not in resp.text and "nan-ish" not in resp.text
    assert body["errors"] == [{"loc": ["body", "count"], "msg": body["errors"][0]["msg"]}]


def test_oversized_body_is_rejected(client: TestClient) -> None:
    big = b"x" * (MAX_BODY_BYTES + 1)
    _assert_problem(
        client.post("/echo", content=big, headers={"content-type": "application/json"}),
        413,
        "request.too_large",
    )


def test_oversized_chunked_body_is_rejected(client: TestClient) -> None:
    chunks = (b"x" * 65536 for _ in range(MAX_BODY_BYTES // 65536 + 2))
    _assert_problem(
        client.post("/echo", content=chunks, headers={"content-type": "application/json"}),
        413,
        "request.too_large",
    )


def test_readyz_reports_failure_without_details(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "fail"}
