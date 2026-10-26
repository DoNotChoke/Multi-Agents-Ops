"""The lifecycle sweep (opsteam_system) learns which incidents to look at, and nothing more."""

from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    # The definer (migrator) may list incidents; the sweep can only call this function and
    # then works on each one inside that tenant's RLS session.
    op.execute("""
        create policy lifecycle_lookup on incidents for select
          to opsteam_migrator using (true)
    """)
    op.execute("""
        create function app_lifecycle_candidates()
        returns table (tenant_id text, incident_id text)
        language sql stable security definer set search_path = public, pg_temp
        as $$
          select i.tenant_id, i.incident_id from incidents i
          where i.status <> 'CLOSED' order by i.opened_at
        $$
    """)
    op.execute("revoke all on function app_lifecycle_candidates() from public")
    op.execute("grant execute on function app_lifecycle_candidates() to opsteam_system")


def downgrade() -> None:
    op.execute("drop function app_lifecycle_candidates()")
    op.execute("drop policy lifecycle_lookup on incidents")
