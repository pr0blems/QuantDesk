"""Register battle ensemble v2 with twelve K-line strategy features.

Revision ID: 0035_battle_kline_features
Revises: 0034_underlying_quotes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_battle_kline_features"
down_revision: str | None = "0034_underlying_quotes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_KEY = "battle-ensemble"
MODEL_VERSION = 2
FEATURE_SCHEMA_VERSION = 5


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _delete_model_version_data() -> None:
    # battle_predictions owns the version FK and cascades to its dependent
    # outcomes (and algorithm snapshots once revision 0036 is present).
    op.execute(
        sa.text(
            "DELETE FROM battle_predictions "
            "WHERE model_key=:model_key AND model_version=:model_version"
        ).bindparams(model_key=MODEL_KEY, model_version=MODEL_VERSION)
    )
    op.execute(
        sa.text(
            "DELETE feature FROM prediction_feature_snapshots AS feature "
            "LEFT JOIN battle_predictions AS prediction "
            "ON prediction.feature_snapshot_id=feature.id "
            "WHERE feature.feature_schema_version=:feature_schema_version "
            "AND prediction.id IS NULL"
        ).bindparams(feature_schema_version=FEATURE_SCHEMA_VERSION)
    )


def upgrade() -> None:
    _require_mysql()
    bind = op.get_bind()
    feature_indexes: dict[str, list[tuple[int, str]]] = {}
    for row in bind.execute(sa.text("SHOW INDEX FROM prediction_feature_snapshots")):
        mapping = row._mapping
        if int(mapping["Non_unique"]) == 0 and str(mapping["Key_name"]) != "PRIMARY":
            feature_indexes.setdefault(str(mapping["Key_name"]), []).append(
                (int(mapping["Seq_in_index"]), str(mapping["Column_name"]))
            )
    normalized_indexes = {
        name: tuple(column for _, column in sorted(columns))
        for name, columns in feature_indexes.items()
    }
    expected_feature_columns = ("symbol", "as_of_ms", "feature_schema_version")
    for name, columns in normalized_indexes.items():
        if columns == ("symbol", "as_of_ms"):
            op.drop_constraint(name, "prediction_feature_snapshots", type_="unique")
    if expected_feature_columns not in normalized_indexes.values():
        op.create_unique_constraint(
            "uq_prediction_feature_symbol_time_schema",
            "prediction_feature_snapshots",
            list(expected_feature_columns),
        )

    primary_columns = {
        str(row._mapping["Column_name"])
        for row in bind.execute(
            sa.text("SHOW KEYS FROM prediction_model_versions WHERE Key_name='PRIMARY'")
        )
    }
    if "version" not in primary_columns:
        # Early local installs keyed the registry only by model_key. Preserve
        # compatibility with their existing battle_predictions foreign key.
        op.execute(
            sa.text(
                "UPDATE prediction_model_versions "
                "SET version=:model_version,feature_schema_version=:feature_schema_version,"
                "state='heuristic',updated_at=CURRENT_TIMESTAMP WHERE model_key=:model_key"
            ).bindparams(
                model_key=MODEL_KEY,
                model_version=MODEL_VERSION,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
            )
        )
        return
    op.execute(
        f"""INSERT IGNORE INTO prediction_model_versions(
               model_key,version,state,feature_schema_version,training_window_json,
               metrics_json,created_at,updated_at)
           VALUES('{MODEL_KEY}',{MODEL_VERSION},'heuristic',{FEATURE_SCHEMA_VERSION},
                  JSON_OBJECT(),JSON_OBJECT(),CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
    )


def downgrade() -> None:
    _require_mysql()
    bind = op.get_bind()
    primary_columns = {
        str(row._mapping["Column_name"])
        for row in bind.execute(
            sa.text("SHOW KEYS FROM prediction_model_versions WHERE Key_name='PRIMARY'")
        )
    }
    _delete_model_version_data()
    if "version" not in primary_columns:
        op.execute(
            sa.text(
                "UPDATE prediction_model_versions "
                "SET version=1,feature_schema_version=4,updated_at=CURRENT_TIMESTAMP "
                "WHERE model_key=:model_key"
            ).bindparams(model_key=MODEL_KEY)
        )
        return
    op.execute(
        sa.text(
            "DELETE FROM prediction_model_versions "
            "WHERE model_key=:model_key AND version=:model_version"
        ).bindparams(model_key=MODEL_KEY, model_version=MODEL_VERSION)
    )
