"""Integrations, service catalog, runners, audit events and the membership lookup."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, grant_app, tenant_rls

revision = "0002"
down_revision = "0001"

TABLES = ("runners", "integrations", "services", "service_dependencies", "audit_events")


def upgrade() -> None:
    # Foreign keys include tenant_id so a row can never point at another tenant's row.
    op.execute(r"""
        create table runners (
          runner_id text primary key check (runner_id like 'rnr\_%'),
          tenant_id text not null references tenants on delete cascade,
          name text not null,
          status text not null check (status in ('pending', 'active', 'revoked')),
          cert_fingerprint text,
          last_heartbeat_at timestamptz,
          version text,
          created_at timestamptz not null default now(),
          unique (tenant_id, runner_id)
        )
    """)
    op.execute(r"""
        create table integrations (
          integration_id text primary key check (integration_id like 'int\_%'),
          tenant_id text not null references tenants on delete cascade,
          type text not null,
          name text not null,
          endpoint_ref text,
          credentials_ref text,
          mode text not null check (mode in ('direct', 'runner')),
          runner_id text,
          status text not null default 'active' check (status in ('active', 'disabled')),
          last_tested_at timestamptz,
          created_at timestamptz not null default now(),
          unique (tenant_id, name),
          foreign key (tenant_id, runner_id) references runners (tenant_id, runner_id)
        )
    """)
    op.execute(r"""
        create table services (
          service_id text primary key check (service_id like 'svc\_%'),
          tenant_id text not null references tenants on delete cascade,
          name text not null,
          environment text not null,
          owner text,
          repository text,
          k8s_workload jsonb,
          datasources jsonb,
          criticality text check (criticality in ('low', 'medium', 'high', 'critical')),
          created_at timestamptz not null default now(),
          unique (tenant_id, name, environment),
          unique (tenant_id, service_id)
        )
    """)
    op.execute("""
        create table service_dependencies (
          tenant_id text not null,
          service_id text not null,
          depends_on_service_id text not null check (depends_on_service_id <> service_id),
          created_at timestamptz not null default now(),
          primary key (tenant_id, service_id, depends_on_service_id),
          foreign key (tenant_id, service_id)
            references services (tenant_id, service_id) on delete cascade,
          foreign key (tenant_id, depends_on_service_id)
            references services (tenant_id, service_id) on delete cascade
        )
    """)
    op.execute(r"""
        create table audit_events (
          audit_id text primary key check (audit_id like 'aud\_%'),
          tenant_id text not null references tenants,
          actor jsonb not null,
          action text not null,
          resource jsonb not null,
          outcome text not null,
          request_id text,
          at timestamptz not null default now(),
          prev_hash text,
          row_hash text not null
        )
    """)
    op.execute("create index audit_events_tenant_at on audit_events (tenant_id, at)")
    for table in TABLES:
        for statement in tenant_rls(table):
            op.execute(statement)
    op.execute(grant_app(*TABLES[:-1]))
    op.execute(f"grant select, insert on audit_events to {APP_ROLE}")  # append-only

    # Lets the API find a user's tenants before any tenant is trusted. The definer (migrator)
    # gets a read-only policy on memberships; the app can only call this function.
    op.execute("""
        create policy membership_lookup on memberships for select
          to opsteam_migrator using (true)
    """)
    op.execute("""
        create function app_memberships_for(p_user_id text)
        returns table (tenant_id text, roles text[])
        language sql stable security definer set search_path = public, pg_temp
        as $$ select m.tenant_id, m.roles from memberships m where m.user_id = p_user_id $$
    """)
    op.execute("revoke all on function app_memberships_for(text) from public")
    op.execute(f"grant execute on function app_memberships_for(text) to {APP_ROLE}")


def downgrade() -> None:
    op.execute("drop function app_memberships_for(text)")
    op.execute("drop policy membership_lookup on memberships")
    for table in reversed(TABLES):
        op.execute(f"drop table {table}")
