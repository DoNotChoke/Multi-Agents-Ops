"""Integrations keep their endpoint and non-secret settings; credentials stay in secrets."""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.execute("alter table integrations rename column endpoint_ref to endpoint")
    op.execute("alter table integrations add column config jsonb not null default '{}'")


def downgrade() -> None:
    op.execute("alter table integrations drop column config")
    op.execute("alter table integrations rename column endpoint to endpoint_ref")
