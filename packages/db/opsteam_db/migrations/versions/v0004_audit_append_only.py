"""Make audit_events append-only even for its owner, and give the hash chain a strict order."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    # Assigned at insert time, after the per-tenant lock in audit.record, so it is the chain order.
    op.execute("alter table audit_events add column seq bigint generated always as identity")
    op.execute("create index audit_events_tenant_seq on audit_events (tenant_id, seq)")
    op.execute("""
        create function audit_append_only() returns trigger language plpgsql as $$
        begin
          raise exception 'audit_events is append-only';
        end $$
    """)
    op.execute("""
        create trigger audit_append_only before update or delete on audit_events
          for each row execute function audit_append_only()
    """)
    op.execute("""
        create trigger audit_no_truncate before truncate on audit_events
          for each statement execute function audit_append_only()
    """)


def downgrade() -> None:
    op.execute("drop trigger audit_no_truncate on audit_events")
    op.execute("drop trigger audit_append_only on audit_events")
    op.execute("drop function audit_append_only()")
    op.execute("drop index audit_events_tenant_seq")
    op.execute("alter table audit_events drop column seq")
