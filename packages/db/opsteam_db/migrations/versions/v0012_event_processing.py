"""The Incident Manager marks events processed and records which service they belong to."""

from alembic import op
from opsteam_db.ddl import APP_ROLE

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.execute(f"grant update (service_ref, processed_at) on events to {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"revoke update (service_ref, processed_at) on events from {APP_ROLE}")
