"""Tool calls and the evidence they produced; history, so the app only inserts and reads."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, tenant_rls

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.execute("alter table integrations add unique (tenant_id, integration_id)")
    # incident_id and run_id have no FK yet: incidents and agent runs arrive in later phases.
    op.execute(r"""
        create table tool_calls (
          tool_call_id text primary key check (tool_call_id like 'tc\_%'),
          tenant_id text not null references tenants on delete cascade,
          run_id text,
          incident_id text,
          tool text not null,
          integration_id text,
          request jsonb not null,
          status text not null
            check (status in ('ok', 'denied', 'error', 'timeout', 'limit_exceeded')),
          duration_ms integer not null default 0,
          bytes integer not null default 0,
          truncated boolean not null default false,
          policy_decision jsonb,
          task_id text,
          created_at timestamptz not null default now(),
          unique (tenant_id, tool_call_id),
          foreign key (tenant_id, integration_id)
            references integrations (tenant_id, integration_id)
            on delete set null (integration_id)
        )
    """)
    op.execute("create index on tool_calls (tenant_id, incident_id)")
    op.execute(r"""
        create table evidence (
          evidence_id text primary key check (evidence_id like 'ev\_%'),
          tenant_id text not null references tenants on delete cascade,
          incident_id text,
          integration_id text,
          source_type text not null check (source_type in
            ('metrics', 'logs', 'traces', 'k8s', 'deployment', 'audit', 'probe', 'derived')),
          tool text not null,
          tool_call_id text not null,
          source_query jsonb not null,
          time_window jsonb not null,
          observed_at timestamptz,
          collected_at timestamptz not null,
          summary text not null check (length(summary) <= 2000),
          content_hash text not null check (content_hash ~ '^sha256:[0-9a-f]{64}$'),
          redaction_status text not null
            check (redaction_status in ('none_needed', 'redacted', 'redaction_failed')),
          truncated boolean not null,
          payload_reference text,
          schema_version integer not null default 1,
          unique (tenant_id, content_hash, tool_call_id),
          check ((redaction_status = 'redaction_failed') = (payload_reference is null)),
          foreign key (tenant_id, tool_call_id) references tool_calls (tenant_id, tool_call_id),
          foreign key (tenant_id, integration_id)
            references integrations (tenant_id, integration_id)
            on delete set null (integration_id)
        )
    """)
    op.execute("create index on evidence (tenant_id, incident_id)")
    for table in ("tool_calls", "evidence"):
        for statement in tenant_rls(table):
            op.execute(statement)
        op.execute(f"grant select, insert on {table} to {APP_ROLE}")


def downgrade() -> None:
    op.execute("drop table evidence")
    op.execute("drop table tool_calls")
    op.execute("alter table integrations drop constraint integrations_tenant_id_integration_id_key")
