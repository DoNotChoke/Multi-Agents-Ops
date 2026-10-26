"""The relay (opsteam_system) reads every tenant's outbox and may only mark rows published."""

from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.execute("""
        create policy relay on outbox to opsteam_system using (true) with check (true)
    """)
    op.execute("grant select, update (published_at) on outbox to opsteam_system")


def downgrade() -> None:
    op.execute("revoke select, update (published_at) on outbox from opsteam_system")
    op.execute("drop policy relay on outbox")
