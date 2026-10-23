"""Hide users outside the current tenant and create users only through app_user_for."""

from alembic import op
from opsteam_db.ddl import APP_ROLE

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("alter table users enable row level security")
    op.execute("alter table users force row level security")
    # memberships is itself tenant-scoped, so this shows only members of the current tenant.
    op.execute("""
        create policy member_of_current_tenant on users for select
          using (exists (select 1 from memberships m where m.user_id = users.user_id))
    """)
    op.execute(
        "create policy identity_admin on users to opsteam_migrator using (true) with check (true)"
    )
    op.execute(f"revoke insert, update, delete on users from {APP_ROLE}")

    # Called with the token's issuer and subject before any tenant is known; p_new_id is used
    # only when the user does not exist yet.
    op.execute(r"""
        create function app_user_for(p_issuer text, p_subject text, p_email text, p_new_id text)
        returns text language plpgsql security definer set search_path = public, pg_temp
        as $$
        declare uid text;
        begin
          select user_id into uid from users
            where oidc_issuer = p_issuer and oidc_subject = p_subject;
          if uid is null then
            insert into users (user_id, oidc_issuer, oidc_subject, email)
              values (p_new_id, p_issuer, p_subject, p_email)
              on conflict (oidc_issuer, oidc_subject) do nothing;
            select user_id into uid from users
              where oidc_issuer = p_issuer and oidc_subject = p_subject;
          end if;
          return uid;
        end $$
    """)
    op.execute("revoke all on function app_user_for(text, text, text, text) from public")
    op.execute(f"grant execute on function app_user_for(text, text, text, text) to {APP_ROLE}")


def downgrade() -> None:
    op.execute("drop function app_user_for(text, text, text, text)")
    op.execute(f"grant insert, update, delete on users to {APP_ROLE}")
    op.execute("drop policy identity_admin on users")
    op.execute("drop policy member_of_current_tenant on users")
    op.execute("alter table users no force row level security")
    op.execute("alter table users disable row level security")
