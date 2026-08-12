"""add outcomes table

Predictions are derived from TripState and never stored, so this is the only
new table: the durable record of times a number met reality. No trip_id and no
coordinates - the corpus has to outlive the trips that produced it, and it must
not become a travel history while doing so.

Revision ID: b3e7c91a45d2
Revises: f2a91c04be77
Create Date: 2026-08-12 04:20:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b3e7c91a45d2'
down_revision: str | None = 'f2a91c04be77'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('outcomes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('dimension', sa.String(length=48), nullable=False),
    sa.Column('mode', sa.String(length=24), nullable=True),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('payload_jsonb', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('outcomes', schema=None) as batch_op:
        batch_op.create_index(
            'ix_outcomes_key', ['provider', 'dimension', 'mode', 'scope'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('outcomes', schema=None) as batch_op:
        batch_op.drop_index('ix_outcomes_key')

    op.drop_table('outcomes')
