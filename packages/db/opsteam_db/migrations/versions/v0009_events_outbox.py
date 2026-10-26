"""Normalized events, the outbox written with them, and the webhook's tenant lookup."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, tenant_rls

revision = "0009"
down_revision = "0008"

SYSTEM_ROLE = "opsteam_system"


def upgrade() -> None:
    op.execute(r"""
        create table events (
          event_id text primary key check (event_id like 'evt\_%'),
          tenant_id text not null references tenants on delete cascade,
          integration_id text,
          source_type text not null
            check (source_type in ('alertmanager', 'loki_ruler', 'k8s_watch', 'probe', 'user')),
          fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
          status text not null check (status in ('firing', 'resolved')),
          severity_hint text check (severity_hint in ('info', 'warning', 'critical')),
          service_ref jsonb not null default '{}',
          title text not null check (length(title) <= 200),
          labels jsonb not null,
          annotations jsonb not null,
          starts_at timestamptz not null,
          ends_at timestamptz,
          received_at timestamptz not null,
          raw_payload_ref text,
          processed_at timestamptz,
          unique (tenant_id, integration_id, fingerprint, status, starts_at),
          foreign key (tenant_id, integration_id)
            references integrations (tenant_id, integration_id)
            on delete set null (integration_id)
        )
    """)
    op.execute("create index on events (tenant_id, received_at)")
    op.execute("create index on events (tenant_id, fingerprint)")
    op.execute("""
        create table outbox (
          outbox_id bigint generated always as identity primary key,
          tenant_id text not null references tenants on delete cascade,
          topic text not null,
          payload jsonb not null,
          created_at timestamptz not null default now(),
          published_at timestamptz
        )
    """)
    op.execute("create index outbox_unpublished on outbox (outbox_id) where published_at is null")
    for table in ("events", "outbox"):
        for statement in tenant_rls(table):
            op.execute(statement)
        op.execute(f"grant select, insert on {table} to {APP_ROLE}")

    # The webhook learns its tenant from the integration id before any tenant is trusted.
    # The definer (migrator) may read Alertmanager integrations; ingest can only call this.
    op.execute("""
        create policy webhook_lookup on integrations for select
          to opsteam_migrator using (type = 'alertmanager')
    """)
    op.execute("""
        create function app_webhook_target(p_integration_id text)
        returns table (tenant_id text, credentials_ref text, status text, config jsonb)
        language sql stable security definer set search_path = public, pg_temp
        as $$
          select i.tenant_id, i.credentials_ref, i.status, i.config from integrations i
          where i.integration_id = p_integration_id and i.type = 'alertmanager'
        $$
    """)
    op.execute("revoke all on function app_webhook_target(text) from public")
    op.execute(f"grant execute on function app_webhook_target(text) to {SYSTEM_ROLE}")


def downgrade() -> None:
    op.execute("drop function app_webhook_target(text)")
    op.execute("drop policy webhook_lookup on integrations")
    op.execute("drop table outbox")
    op.execute("drop table events")
