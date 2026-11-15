"""Metadados das gravações pendentes e versões publicadas.

O banco registra intenções e confirmações. Bytes reais serão armazenados
nos Storage Nodes, nunca no PostgreSQL.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column

from controller.models import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Directory(Base):
    __tablename__ = "directories"

    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )


class FileEntry(Base):
    __tablename__ = "files"

    file_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True, index=True)
    # Referência da versão visível; nova escrita nunca altera o ponteiro até o commit.
    current_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_file_versions_number"),)

    version_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.file_id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    replication_factor: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Replica(Base):
    __tablename__ = "replicas"
    __table_args__ = (
        UniqueConstraint("version_id", "node_id", name="uq_replica_per_node"),
        UniqueConstraint("version_id", "disk_id", name="uq_replica_per_disk"),
    )

    replica_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("file_versions.version_id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("nodes.node_id"), nullable=False
    )
    disk_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("disks.disk_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
