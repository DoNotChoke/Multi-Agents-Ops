"""Database access; `tenant_session` is the only way to get a session."""

from opsteam_db.session import create_engine, tenant_session

__all__ = ["create_engine", "tenant_session"]
