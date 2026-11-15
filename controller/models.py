"""Metadados de descoberta e capacidade do cluster."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disks: Mapped[list["Disk"]] = relationship(
        back_populates="node", cascade="all, delete-orphan", lazy="selectin"
    )


class Disk(Base):
    __tablename__ = "disks"

    disk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False, index=True
    )
    capacity_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node: Mapped[Node] = relationship(back_populates="disks")
