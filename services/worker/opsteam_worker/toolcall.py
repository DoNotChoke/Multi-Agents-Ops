"""Dev-only CLI: run one metrics tool call through the full Gateway and print the ToolResult."""

import argparse
import asyncio
import sys
from pathlib import Path

from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.config import Settings
from opsteam_core.context import Principal, RequestContext
from opsteam_db import create_engine
from opsteam_db.secrets import DbSecretStore
from opsteam_evidence.objects import S3ObjectStore
from opsteam_policy.client import PolicyClient, bundle_version
from opsteam_tools.backends import DirectBackend
from opsteam_tools.gateway.executor import GatewayToolExecutor
from opsteam_tools.gateway.pipeline import Gateway
from pydantic import SecretStr
from redis.asyncio import Redis

POLICIES = Path(__file__).parents[3] / "policies"


class ToolcallSettings(Settings):
    database_url: str
    secrets_key: SecretStr
    redis_url: SecretStr
    opa_url: str = "http://127.0.0.1:8181"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: str
    s3_secret_key: SecretStr
    s3_bucket: str = "evidence"


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="toolcall", description=__doc__)
    p.add_argument("--tenant", required=True)
    p.add_argument("--integration", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--environment", required=True)
    p.add_argument("--template", default="http_latency_p95")
    p.add_argument("--window", type=int, default=3600, help="seconds")
    p.add_argument("--step", type=int, default=60, help="seconds")
    p.add_argument("--incident", help="inc_… to charge the call to; ad-hoc when absent")
    return p.parse_args(argv)


async def run(args: argparse.Namespace, settings: ToolcallSettings) -> str:
    engine = create_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    policy = PolicyClient(settings.opa_url, bundle_version(POLICIES))
    try:
        gateway = Gateway(engine, DbSecretStore(engine, settings.secrets_key), policy, redis)
        executor = GatewayToolExecutor(
            gateway,
            DirectBackend(dev_allow=settings.netguard_dev_allow),
            S3ObjectStore(
                settings.s3_endpoint,
                settings.s3_access_key,
                settings.s3_secret_key,
                settings.s3_bucket,
            ),
        )
        ctx = RequestContext(
            tenant_id=args.tenant,
            principal=Principal(type="system", id="toolcall-cli"),
            roles=(),
            scopes=frozenset(),
            incident_id=args.incident,
        )
        params = {"service": args.service, "environment": args.environment}
        request = ToolRequest.model_validate(
            {
                "tool": "metrics.query_range",
                "integration_id": args.integration,
                "resource": params,
                "arguments": {
                    "query_template": args.template,
                    "params": params,
                    "window_seconds": args.window,
                    "step_seconds": args.step,
                },
            }
        )
        answer = await executor.execute(ctx, request, agent="metrics")
        return answer.model_dump_json(indent=2, exclude_none=True)
    finally:
        await policy.aclose()
        await redis.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    settings = ToolcallSettings()  # type: ignore[call-arg]
    if settings.env != "dev":
        sys.stderr.write("toolcall runs only with OPSTEAM_ENV=dev\n")
        return 2
    sys.stdout.write(asyncio.run(run(parse(argv or sys.argv[1:]), settings)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
