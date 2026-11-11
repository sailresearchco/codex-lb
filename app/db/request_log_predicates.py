"""Stable request eligibility shared by indexes and usage queries."""

from sqlalchemy import ColumnElement, and_, literal
from sqlalchemy.sql import FromClause

ACCOUNT_USAGE_EXCLUDED_KINDS = ("warmup", "limit_warmup")


def countable_account_usage_clause(table: FromClause) -> ColumnElement[bool]:
    # These schema constants must be literal SQL so SQLite and PostgreSQL can
    # prove the query implies the partial-index predicate, including prepared
    # statements. Account IDs, timestamps and other caller inputs stay bound.
    return and_(
        table.c.deleted_at.is_(None),
        table.c.account_id.is_not(None),
        table.c.request_kind.not_in([literal(kind, literal_execute=True) for kind in ACCOUNT_USAGE_EXCLUDED_KINDS]),
    )
