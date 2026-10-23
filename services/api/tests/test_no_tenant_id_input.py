"""Route handlers must take the tenant from RequestContext, never from a parameter."""

import ast
from pathlib import Path

import opsteam_api

ROUTE_METHODS = {"get", "post", "put", "patch", "delete"}
PACKAGE = Path(opsteam_api.__file__).parent


def _route_handlers() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    handlers = []
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in ROUTE_METHODS
                for d in node.decorator_list
            ):
                handlers.append(node)
    return handlers


def test_route_handlers_exist() -> None:
    assert {h.name for h in _route_handlers()} >= {"me", "tenant", "healthz", "readyz"}


def test_no_route_handler_takes_tenant_id() -> None:
    offenders = [
        h.name
        for h in _route_handlers()
        for arg in [*h.args.args, *h.args.kwonlyargs]
        if "tenant_id" in arg.arg
    ]
    assert offenders == []
