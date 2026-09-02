"""create_recovery_audit_events_table

Revision ID: b2de3857a48d
Revises: a1ceb094e27c
Create Date: 2026-09-01 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2de3857a48d'
down_revision: Union[str, Sequence[str], None] = 'a1ceb094e27c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recovery_audit_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('merchant_id', sa.UUID(), nullable=False),
        sa.Column('incident_id', sa.UUID(), nullable=True),
        sa.Column('campaign_id', sa.UUID(), nullable=True),
        sa.Column('recovery_attempt_id', sa.UUID(), nullable=True),
        sa.Column('payment_id', sa.UUID(), nullable=True),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('actor_type', sa.String(length=32), nullable=False),
        sa.Column('actor_id', sa.String(length=128), nullable=True),
        sa.Column('previous_state', sa.String(length=32), nullable=True),
        sa.Column('new_state', sa.String(length=32), nullable=True),
        sa.Column('reason_code', sa.String(length=64), nullable=True),
        sa.Column('explanation', sa.Text(), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['recovery_campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recovery_attempt_id'], ['recovery_attempts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_recovery_audit_events_merchant_id'), 'recovery_audit_events', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_incident_id'), 'recovery_audit_events', ['incident_id'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_campaign_id'), 'recovery_audit_events', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_recovery_attempt_id'), 'recovery_audit_events', ['recovery_attempt_id'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_payment_id'), 'recovery_audit_events', ['payment_id'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_event_type'), 'recovery_audit_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_recovery_audit_events_created_at'), 'recovery_audit_events', ['created_at'], unique=False)
    op.create_index('ix_recovery_audit_events_merchant_created', 'recovery_audit_events', ['merchant_id', 'created_at'], unique=False)
    op.create_index('ix_recovery_audit_events_campaign_created', 'recovery_audit_events', ['campaign_id', 'created_at'], unique=False)
    op.create_index('ix_recovery_audit_events_attempt_created', 'recovery_audit_events', ['recovery_attempt_id', 'created_at'], unique=False)
    op.create_index('ix_recovery_audit_events_payment_created', 'recovery_audit_events', ['payment_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_recovery_audit_events_payment_created', table_name='recovery_audit_events')
    op.drop_index('ix_recovery_audit_events_attempt_created', table_name='recovery_audit_events')
    op.drop_index('ix_recovery_audit_events_campaign_created', table_name='recovery_audit_events')
    op.drop_index('ix_recovery_audit_events_merchant_created', table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_created_at'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_event_type'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_payment_id'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_recovery_attempt_id'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_campaign_id'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_incident_id'), table_name='recovery_audit_events')
    op.drop_index(op.f('ix_recovery_audit_events_merchant_id'), table_name='recovery_audit_events')
    op.drop_table('recovery_audit_events')
