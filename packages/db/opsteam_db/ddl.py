"""SQL statements shared by migrations; asyncpg runs one statement per execute."""

APP_ROLE = "opsteam_app"


def tenant_rls(table: str) -> list[str]:
    """Return statements that limit every read and write on `table` to the session's tenant."""
    return [
        f"alter table {table} enable row level security",
        f"alter table {table} force row level security",
        f"""create policy tenant_isolation on {table}
              using (tenant_id = current_setting('app.tenant_id', true))
              with check (tenant_id = current_setting('app.tenant_id', true))""",
    ]


def grant_app(*tables: str) -> str:
    """Return a statement granting the app role row access (not DDL) on `tables`."""
    return f"grant select, insert, update, delete on {', '.join(tables)} to {APP_ROLE}"
