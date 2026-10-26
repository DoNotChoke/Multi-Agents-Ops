"""Tenant rules that map alert labels to a catalog service when the labels alone cannot."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, tenant_rls

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.execute(r"""
        create table service_mappings (
          mapping_id text primary key check (mapping_id like 'rule\_%'),
          tenant_id text not null references tenants on delete cascade,
          -- Exact label = value pairs; an empty object would match every alert.
          matchers jsonb not null
            check (jsonb_typeof(matchers) = 'object' and matchers <> '{}'::jsonb),
          service_id text not null,
          priority integer not null default 0,
          created_by text not null,
          created_at timestamptz not null default now(),
          unique (tenant_id, matchers),
          foreign key (tenant_id, service_id) references services (tenant_id, service_id)
            on delete cascade
        )
    """)
    for statement in tenant_rls("service_mappings"):
        op.execute(statement)
    op.execute(f"grant select, insert, delete on service_mappings to {APP_ROLE}")


def downgrade() -> None:
    op.execute("drop table service_mappings")
