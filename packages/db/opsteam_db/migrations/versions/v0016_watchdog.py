"""Watchdog heartbeats on Alertmanager integrations and the sweep's view of them."""

from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.execute("alter table integrations add column last_heartbeat_at timestamptz")
    op.execute("alter table incidents drop constraint incidents_source_check")
    op.execute("""
        alter table incidents add constraint incidents_source_check
          check (source in ('alert', 'user', 'probe', 'watchdog'))
    """)
    # Runs as the migrator, which may read Alertmanager integrations (policy webhook_lookup);
    # the sweep then acts on each one inside that tenant's RLS session.
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


def downgrade() -> None:
    op.execute("drop function app_watchdog_candidates()")
    op.execute("delete from incidents where source = 'watchdog'")
    op.execute("alter table incidents drop constraint incidents_source_check")
    op.execute("""
        alter table incidents add constraint incidents_source_check
          check (source in ('alert', 'user', 'probe'))
    """)
    op.execute("alter table integrations drop column last_heartbeat_at")
