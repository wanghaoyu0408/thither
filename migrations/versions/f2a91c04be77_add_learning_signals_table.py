"""add learning_signals table

Revision ID: f2a91c04be77
Revises: a8defcdff070
Create Date: 2026-08-09 21:40:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f2a91c04be77'
down_revision: str | None = 'a8defcdff070'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('learning_signals',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('profile_id', sa.String(length=64), nullable=False),
    sa.Column('trip_id', sa.String(length=64), nullable=False),
    sa.Column('payload_jsonb', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('learning_signals', schema=None) as batch_op:
        batch_op.create_index('ix_learning_signals_profile_id', ['profile_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('learning_signals', schema=None) as batch_op:
        batch_op.drop_index('ix_learning_signals_profile_id')

    op.drop_table('learning_signals')
