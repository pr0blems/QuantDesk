"""Compatibility marker for a removed data-quality control-plane revision.

Revision ID: 0061_data_quality_control_plane
Revises: 0060_query_path_indexes

The application was intentionally rolled back while deployed databases had
already applied this revision.  Current code does not depend on the removed
control-plane tables, so this marker preserves Alembic lineage without
recreating retired schema on fresh installations.
"""

from collections.abc import Sequence

revision: str = "0061_data_quality_control_plane"
down_revision: str | None = "0060_query_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
