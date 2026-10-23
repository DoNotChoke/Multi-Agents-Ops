"""Encrypted credential storage for the dev SecretStore; rows are opaque ciphertext."""

from alembic import op
from opsteam_db.ddl import grant_app, tenant_rls

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute(r"""
        create table secrets (
          credentials_ref text primary key check (credentials_ref like 'sec\_%'),
          tenant_id text not null references tenants on delete cascade,
          ciphertext bytea not null,
          created_at timestamptz not null default now(),
          rotated_at timestamptz
        )
    """)
    for statement in tenant_rls("secrets"):
        op.execute(statement)
    op.execute(grant_app("secrets"))


def downgrade() -> None:
    op.execute("drop table secrets")
