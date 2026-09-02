"""unique_recovery_campaign_incident_id

Revision ID: e5b12a88a101
Revises: b2de3857a48d
Create Date: 2026-09-01 22:15:00.000000

"""
from typing import Sequence, Union
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b12a88a101'
down_revision: Union[str, Sequence[str], None] = 'b2de3857a48d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        'uq_recovery_campaigns_incident_id',
        'recovery_campaigns',
        ['incident_id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_recovery_campaigns_incident_id',
        'recovery_campaigns',
        type_='unique',
    )

