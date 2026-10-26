"""Incidents, the events attached to them, their lifecycle history and the activity feed."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, tenant_rls

revision = "0011"
down_revision = "0010"

STATES = (
    "NEW", "TRIAGED", "INVESTIGATING", "DIAGNOSED", "INCONCLUSIVE", "REMEDIATION_PROPOSED",
    "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "MANUAL_ACTION_REQUIRED", "RESOLVED", "CLOSED",
)  # fmt: skip
TABLES = ("incidents", "incident_events", "incident_transitions", "incident_activity")


def upgrade() -> None:
    op.execute("alter table events add unique (tenant_id, event_id)")
    states = ", ".join(f"'{s}'" for s in STATES)
    op.execute(rf"""
        create table incidents (
          incident_id text primary key check (incident_id like 'inc\_%'),
          tenant_id text not null references tenants on delete cascade,
          service_id text,
          -- Raw name/environment from the labels when no catalog service matched.
          unmapped_service jsonb,
          source text not null check (source in ('alert', 'user', 'probe')),
          title text not null check (length(title) <= 200),
          status text not null check (status in ({states})),
          severity text not null check (severity in ('info', 'low', 'medium', 'high', 'critical')),
          root_fingerprint text,
          opened_at timestamptz not null,
          detected_at timestamptz not null,
          first_notified_at timestamptz,
          first_hypothesis_at timestamptz,
          resolved_at timestamptz,
          workflow_id text,
          investigation_request jsonb,
          budget jsonb,
          summary text,
          created_by text not null,
          unique (tenant_id, incident_id),
          foreign key (tenant_id, service_id) references services (tenant_id, service_id)
            on delete set null (service_id)
        )
    """)
    op.execute("create index on incidents (tenant_id, status, opened_at desc)")
    op.execute("""
        create table incident_events (
          incident_id text not null,
          event_id text not null,
          tenant_id text not null references tenants on delete cascade,
          attached_at timestamptz not null default now(),
          primary key (incident_id, event_id),
          foreign key (tenant_id, incident_id) references incidents (tenant_id, incident_id)
            on delete cascade,
          foreign key (tenant_id, event_id) references events (tenant_id, event_id)
        )
    """)
    op.execute(f"""
        create table incident_transitions (
          transition_id bigint generated always as identity primary key,
          tenant_id text not null references tenants on delete cascade,
          incident_id text not null,
          from_status text check (from_status in ({states})),
          to_status text not null check (to_status in ({states})),
          reason text not null check (length(reason) <= 500),
          actor jsonb not null,
          at timestamptz not null,
          foreign key (tenant_id, incident_id) references incidents (tenant_id, incident_id)
            on delete cascade
        )
    """)
    op.execute("create index on incident_transitions (tenant_id, incident_id)")
    op.execute("""
        create table incident_activity (
          seq bigint generated always as identity primary key,
          tenant_id text not null references tenants on delete cascade,
          incident_id text not null,
          type text not null,
          ref_id text,
          at timestamptz not null default now(),
          foreign key (tenant_id, incident_id) references incidents (tenant_id, incident_id)
            on delete cascade
        )
    """)
    op.execute("create index on incident_activity (tenant_id, incident_id, seq)")
    for table in TABLES:
        for statement in tenant_rls(table):
            op.execute(statement)
    op.execute(f"grant select, insert, update on incidents to {APP_ROLE}")
    # History and feeds are append-only for the app.
    for table in TABLES[1:]:
        op.execute(f"grant select, insert on {table} to {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"drop table {table}")
    op.execute("alter table events drop constraint events_tenant_id_event_id_key")
