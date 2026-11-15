"""Metadados de diretórios, arquivos, versões e réplicas.

Revision ID: 0002_file_control
Revises: 0001_nodes_and_disks
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_file_control"
down_revision = "0001_nodes_and_disks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "directories",
        sa.Column("path", sa.String(length=1024), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO directories (path, created_at) VALUES ('/', CURRENT_TIMESTAMP)")
    op.create_table(
        "files",
        sa.Column("file_id", sa.String(length=32), primary_key=True),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("current_version_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("path", name="uq_files_path"),
    )
    op.create_index("ix_files_path", "files", ["path"], unique=True)
    op.create_table(
        "file_versions",
        sa.Column("version_id", sa.String(length=32), primary_key=True),
        sa.Column("file_id", sa.String(length=32), sa.ForeignKey("files.file_id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("replication_factor", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("file_id", "version", name="uq_file_versions_number"),
    )
    op.create_index("ix_file_versions_file_id", "file_versions", ["file_id"])
    op.create_index("ix_file_versions_status", "file_versions", ["status"])
    op.create_table(
        "replicas",
        sa.Column("replica_id", sa.String(length=32), primary_key=True),
        sa.Column("version_id", sa.String(length=32), sa.ForeignKey("file_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(length=128), sa.ForeignKey("nodes.node_id"), nullable=False),
        sa.Column("disk_id", sa.String(length=128), sa.ForeignKey("disks.disk_id"), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("version_id", "node_id", name="uq_replica_per_node"),
        sa.UniqueConstraint("version_id", "disk_id", name="uq_replica_per_disk"),
    )
    op.create_index("ix_replicas_version_id", "replicas", ["version_id"])


def downgrade() -> None:
    op.drop_index("ix_replicas_version_id", table_name="replicas")
    op.drop_table("replicas")
    op.drop_index("ix_file_versions_status", table_name="file_versions")
    op.drop_index("ix_file_versions_file_id", table_name="file_versions")
    op.drop_table("file_versions")
    op.drop_index("ix_files_path", table_name="files")
    op.drop_table("files")
    op.drop_table("directories")
