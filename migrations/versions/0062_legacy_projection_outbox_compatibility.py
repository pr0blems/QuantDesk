"""Compatibility marker for a removed projection-outbox revision.

Revision ID: 0062_ai_projection_outbox
Revises: 0061_data_quality_control_plane

This keeps databases migrated by the pre-rollback codebase on a valid Alembic
lineage.  The current application does not depend on the retired outbox.
"""

from collections.abc import Sequence

revision: str = "0062_ai_projection_outbox"
down_revision: str | None = "0061_data_quality_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
