"""Investigation history: agent runs, their LLM calls, and the findings and hypotheses they cite."""

from alembic import op
from opsteam_db.ddl import APP_ROLE, tenant_rls

revision = "0018"
down_revision = "0017"

AGENTS = "'commander', 'metrics', 'logs', 'infrastructure', 'verifier', 'reporting'"
STATUSES = "'running', 'completed', 'partial', 'failed', 'budget_exhausted'"
STOP_REASONS = "'done', 'budget', 'deadline', 'error', 'no_data'"
SEVERITIES = "'info', 'low', 'medium', 'high', 'critical'"
CATEGORIES = (
    "'deployment_regression', 'config_change', 'resource_exhaustion', 'dependency_failure',"
    " 'traffic_spike', 'infrastructure_failure', 'external_dependency', 'unknown'"
)
ASSESSMENTS = "'proposed', 'supported', 'weakened', 'refuted', 'inconclusive'"
TABLES = (
    "agent_runs", "llm_calls", "findings", "finding_evidence", "hypotheses", "hypothesis_evidence",
)  # fmt: skip


def upgrade() -> None:
    op.execute(rf"""
        create table agent_runs (
          run_id text primary key check (run_id like 'run\_%'),
          tenant_id text not null references tenants on delete cascade,
          incident_id text not null,
          agent text not null check (agent in ({AGENTS})),
          prompt_version text not null,
          model_alias text not null,
          model_id text,
          status text not null default 'running' check (status in ({STATUSES})),
          stop_reason text check (stop_reason in ({STOP_REASONS})),
          tokens_in integer not null default 0,
          tokens_out integer not null default 0,
          tool_calls integer not null default 0,
          cost_usd numeric(12, 6) not null default 0,
          started_at timestamptz not null default now(),
          finished_at timestamptz,
          error jsonb,
          unique (tenant_id, run_id),
          foreign key (tenant_id, incident_id) references incidents (tenant_id, incident_id)
            on delete cascade
        )
    """)
    op.execute("create index on agent_runs (tenant_id, incident_id)")
    # No prompt or answer columns: only metadata about each call is ever kept.
    op.execute(r"""
        create table llm_calls (
          llm_id text primary key check (llm_id like 'llm\_%'),
          tenant_id text not null references tenants on delete cascade,
          run_id text not null,
          model_id text not null,
          tier text not null check (tier in ('tier1', 'tier2', 'tier3')),
          tokens_in integer not null,
          tokens_out integer not null,
          cached boolean not null,
          cost_usd numeric(12, 6) not null,
          latency_ms integer not null,
          created_at timestamptz not null default now(),
          foreign key (tenant_id, run_id) references agent_runs (tenant_id, run_id)
            on delete cascade
        )
    """)
    op.execute(rf"""
        create table findings (
          finding_id text primary key check (finding_id like 'fnd\_%'),
          tenant_id text not null references tenants on delete cascade,
          incident_id text not null,
          run_id text not null,
          agent text not null check (agent in ({AGENTS})),
          claim text not null check (length(claim) <= 2000),
          severity text not null check (severity in ({SEVERITIES})),
          time_window jsonb,
          created_at timestamptz not null default now(),
          unique (tenant_id, finding_id),
          foreign key (tenant_id, run_id) references agent_runs (tenant_id, run_id)
            on delete cascade
        )
    """)
    op.execute("create index on findings (tenant_id, incident_id)")
    op.execute("alter table evidence add unique (tenant_id, evidence_id)")
    # Every claim cites real evidence of the same tenant, enforced by the keys.
    op.execute("""
        create table finding_evidence (
          finding_id text not null,
          evidence_id text not null,
          tenant_id text not null references tenants on delete cascade,
          primary key (finding_id, evidence_id),
          foreign key (tenant_id, finding_id) references findings (tenant_id, finding_id)
            on delete cascade,
          foreign key (tenant_id, evidence_id) references evidence (tenant_id, evidence_id)
        )
    """)
    op.execute(rf"""
        create table hypotheses (
          hypothesis_id text primary key check (hypothesis_id like 'hyp\_%'),
          tenant_id text not null references tenants on delete cascade,
          incident_id text not null,
          run_id text not null,
          agent text not null check (agent in ({AGENTS})),
          description text not null check (length(description) <= 2000),
          category text not null check (category in ({CATEGORIES})),
          entities jsonb not null default '[]',
          assessment text not null default 'proposed' check (assessment in ({ASSESSMENTS})),
          rank integer,
          verifier_notes text,
          confidence numeric(4, 3),
          missing_data jsonb not null default '[]',
          created_at timestamptz not null default now(),
          unique (tenant_id, hypothesis_id),
          foreign key (tenant_id, run_id) references agent_runs (tenant_id, run_id)
            on delete cascade
        )
    """)
    op.execute("create index on hypotheses (tenant_id, incident_id)")
    op.execute("""
        create table hypothesis_evidence (
          hypothesis_id text not null,
          evidence_id text not null,
          tenant_id text not null references tenants on delete cascade,
          kind text not null check (kind in ('support', 'counter')),
          primary key (hypothesis_id, evidence_id, kind),
          foreign key (tenant_id, hypothesis_id) references hypotheses (tenant_id, hypothesis_id)
            on delete cascade,
          foreign key (tenant_id, evidence_id) references evidence (tenant_id, evidence_id)
        )
    """)
    for table in TABLES:
        for statement in tenant_rls(table):
            op.execute(statement)
        op.execute(f"grant select, insert on {table} to {APP_ROLE}")
    # A run is opened, then closed with its totals; nothing else is rewritten.
    op.execute(
        f"grant update (model_id, status, stop_reason, tokens_in, tokens_out, tool_calls,"
        f" cost_usd, finished_at, error) on agent_runs to {APP_ROLE}"
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"drop table {table}")
    op.execute("alter table evidence drop constraint evidence_tenant_id_evidence_id_key")
