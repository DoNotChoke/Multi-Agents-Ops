"""Reconciliation: which Alertmanagers to read back, and how often in a row that failed."""

from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.execute("alter table integrations add column reconcile_failures int not null default 0")
    op.execute("""
        create function app_reconcile_targets()
        returns table (tenant_id text, integration_id text, endpoint text, config jsonb)
        language sql stable security definer set search_path = public, pg_temp
        as $$
          select i.tenant_id, i.integration_id, i.endpoint, i.config from integrations i
          where i.type = 'alertmanager' and i.status = 'active' and i.endpoint is not null
          order by i.integration_id
        $$
    """)
    op.execute("revoke all on function app_reconcile_targets() from public")
    op.execute("grant execute on function app_reconcile_targets() to opsteam_system")
    # Also integrations that only failed reconciliation, or still have visibility_lost open.
    op.execute("drop function app_watchdog_candidates()")
    op.execute("""
        create function app_watchdog_candidates()
        returns table (tenant_id text, integration_id text, name text, status text,
                       last_heartbeat_at timestamptz, interval_s int, reconcile_failures int)
        language sql stable security definer set search_path = public, pg_temp
        as $$
          select i.tenant_id, i.integration_id, i.name, i.status, i.last_heartbeat_at,
                 coalesce((i.config->>'watchdog_interval_seconds')::int, 300),
                 i.reconcile_failures
          from integrations i
          where i.type = 'alertmanager'
            and (i.last_heartbeat_at is not null or i.reconcile_failures > 0
                 or exists (select 1 from incidents x
                            where x.root_fingerprint = 'watchdog:' || i.integration_id
                              and x.status not in ('RESOLVED', 'CLOSED')))
          order by i.integration_id
        $$
    """)
    op.execute("revoke all on function app_watchdog_candidates() from public")
    op.execute("grant execute on function app_watchdog_candidates() to opsteam_system")


def downgrade() -> None:
    op.execute("drop function app_watchdog_candidates()")
    op.execute("""
        create function app_watchdog_candidates()
        returns table (tenant_id text, integration_id text, name text, status text,
                       last_heartbeat_at timestamptz, interval_s int)
        language sql stable security definer set search_path = public, pg_temp
        as $$
          select i.tenant_id, i.integration_id, i.name, i.status, i.last_heartbeat_at,
                 coalesce((i.config->>'watchdog_interval_seconds')::int, 300)
          from integrations i
          where i.type = 'alertmanager' and i.last_heartbeat_at is not null
          order by i.integration_id
        $$
    """)
    op.execute("revoke all on function app_watchdog_candidates() from public")
    op.execute("grant execute on function app_watchdog_candidates() to opsteam_system")
    op.execute("drop function app_reconcile_targets()")
    op.execute("alter table integrations drop column reconcile_failures")
