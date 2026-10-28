"""Outbox retries: a failing row backs off instead of holding up every other tenant."""

from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.execute("""
        alter table outbox
          add column attempts int not null default 0,
          add column next_attempt_at timestamptz
    """)
    op.execute("grant update (attempts, next_attempt_at) on outbox to opsteam_system")


def downgrade() -> None:
    op.execute("revoke update (attempts, next_attempt_at) on outbox from opsteam_system")
    op.execute("alter table outbox drop column next_attempt_at, drop column attempts")
