"""Inicializa as tabelas nodes e disks.

Revision ID: 0001_nodes_and_disks
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_nodes_and_disks"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nodes",
        sa.Column("node_id", sa.String(length=128), primary_key=True),
        sa.Column("machine_id", sa.String(length=128), nullable=False),
        sa.Column("address", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_nodes_machine_id", "nodes", ["machine_id"])
    op.create_index("ix_nodes_state", "nodes", ["state"])
    op.create_table(
        "disks",
        sa.Column("disk_id", sa.String(length=128), primary_key=True),
        sa.Column("node_id", sa.String(length=128), sa.ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False),
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_disks_node_id", "disks", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_disks_node_id", table_name="disks")
    op.drop_table("disks")
    op.drop_index("ix_nodes_state", table_name="nodes")
    op.drop_index("ix_nodes_machine_id", table_name="nodes")
    op.drop_table("nodes")
