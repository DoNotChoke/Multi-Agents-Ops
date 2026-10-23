"""Let an admin find an existing user by exact email to add them to their tenant."""

from alembic import op
from opsteam_db.ddl import APP_ROLE

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    # Returns only the id; users outside the tenant stay hidden by RLS otherwise.
    op.execute("""
        create function app_user_id_by_email(p_email text) returns text
        language sql stable security definer set search_path = public, pg_temp
        as $$ select user_id from users where email = p_email $$
    """)
    op.execute("revoke all on function app_user_id_by_email(text) from public")
    op.execute(f"grant execute on function app_user_id_by_email(text) to {APP_ROLE}")


def downgrade() -> None:
    op.execute("drop function app_user_id_by_email(text)")
