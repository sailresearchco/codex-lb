"""Offline conversation projection reset after an older writer was used.

Stop ALL request-log writers before invoking this module and keep them stopped
until it succeeds. Interrupted runs resume the persisted cursor. Raw history
is retained; new code backfills its projection after the command completes.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete, select, update

from app.db.models import (
    AccountUsageRollupState,
    RequestConversationFacet,
    RequestConversationModelSummary,
    RequestConversationSummary,
    RequestLog,
)
from app.db.session import engine, get_background_session, sqlite_writer_section
from app.modules.accounts.usage_rollup import lock_fold_state

RESET_BATCH_SIZE = 500


async def reset_next_batch() -> bool:
    """Commit one reset page; True only after final projection invalidation."""
    async with get_background_session() as session:
        async with sqlite_writer_section():
            await lock_fold_state(session)
            cursor = await session.scalar(
                select(AccountUsageRollupState.conversation_reset_after_id).where(AccountUsageRollupState.id == 1)
            )
            if cursor is None:
                await session.execute(
                    update(AccountUsageRollupState)
                    .where(AccountUsageRollupState.id == 1)
                    .values(conversation_reset_after_id=0)
                )
                await session.commit()
                return False
            ids = list(
                (
                    await session.scalars(
                        select(RequestLog.id)
                        .where(RequestLog.id > cursor)
                        .order_by(RequestLog.id)
                        .limit(RESET_BATCH_SIZE)
                    )
                ).all()
            )
            if ids:
                await session.execute(
                    update(RequestLog).where(RequestLog.id.in_(ids)).values(conversation_summary_included=False)
                )
                await session.execute(
                    update(AccountUsageRollupState)
                    .where(AccountUsageRollupState.id == 1)
                    .values(conversation_reset_after_id=ids[-1])
                )
            else:
                for table in (RequestConversationFacet, RequestConversationModelSummary, RequestConversationSummary):
                    await session.execute(delete(table))
                await session.execute(
                    update(AccountUsageRollupState)
                    .where(AccountUsageRollupState.id == 1)
                    .values(conversation_reset_after_id=None)
                )
            await session.commit()
            return not ids


async def reset_conversation_summaries() -> None:
    while not await reset_next_batch():
        pass


async def _run() -> None:
    try:
        await reset_conversation_summaries()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(_run())
    print("Conversation projection reset complete. Start new-code writers to rebuild the summaries.")


if __name__ == "__main__":
    main()
