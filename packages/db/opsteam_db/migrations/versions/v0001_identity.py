"""Identity tables: tenants, users, memberships."""

from alembic import op
from opsteam_db.ddl import grant_app, tenant_rls

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute(r"""
        create table tenants (
          tenant_id text primary key check (tenant_id like 'tnt\_%'),
          name text not null,
          plan text not null default 'dev',
          region text,
          quotas jsonb not null default '{}',
          retention_days int not null default 30 check (retention_days > 0),
          created_at timestamptz not null default now()
        )
    """)
    op.execute(r"""
        create table users (
          user_id text primary key check (user_id like 'usr\_%'),
          email text unique,
          display_name text,
          oidc_issuer text not null,
          oidc_subject text not null,
          created_at timestamptz not null default now(),
          unique (oidc_issuer, oidc_subject)
        )
    """)
    op.execute("""
        create table memberships (
          tenant_id text not null references tenants on delete cascade,
          user_id text not null references users on delete cascade,
          roles text[] not null,
          created_at timestamptz not null default now(),
          primary key (tenant_id, user_id)
        )
    """)
    for statement in [*tenant_rls("tenants"), *tenant_rls("memberships")]:
        op.execute(statement)
    op.execute(grant_app("tenants", "users", "memberships"))


def downgrade() -> None:
    for table in ("memberships", "users", "tenants"):
        op.execute(f"drop table {table}")
