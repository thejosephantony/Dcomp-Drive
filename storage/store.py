"""Mecanismo de armazenamento local de objetos e persistência física."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid

from storage.schemas import ObjectMetadata, ScrubReport, validate_object_id_str

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Exceção base de erros no armazenamento local."""


class ObjectNotFoundError(StorageError):
    """Objeto não encontrado no armazenamento."""


class HashMismatchError(StorageError):
    """O hash SHA-256 do conteúdo gravado diverge do hash esperado."""


class CorruptedObjectError(StorageError):
    """O conteúdo em disco está corrompido (hash diverge dos metadados)."""


class InsufficientStorageError(StorageError):
    """Espaço em disco insuficiente para gravar o objeto."""


class ObjectStore:
    def __init__(
        self,
        data_dir: Path,
        node_id: str,
        disk_id: str,
        capacity_bytes: int | None = None,
        max_disk_usage_percent: int = 90,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.node_id = node_id
        self.disk_id = disk_id
        self._capacity_bytes = capacity_bytes
        self.max_disk_usage_percent = max_disk_usage_percent

        self.objects_dir = self.data_dir / "objects"
        self.meta_dir = self.data_dir / "meta"
        self.tmp_dir = self.data_dir / ".tmp"

        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cache: dict[str, ObjectMetadata] = {}
        self.last_scrub_report: ScrubReport | None = None

        self._cleanup_tmp()
        self._reload_metadata()

    def _cleanup_tmp(self) -> None:
        """Limpa arquivos temporários residuais de inicializações anteriores."""
        try:
            for item in self.tmp_dir.iterdir():
                if item.is_file():
                    item.unlink(missing_ok=True)
        except OSError:
            logger.warning("Falha ao limpar diretório temporário: %s", self.tmp_dir)

    def _reload_metadata(self) -> None:
        """Carrega metadados existentes em disco para reconstruir o estado em memória."""
        self._cache.clear()
        for meta_file in self.meta_dir.glob("*.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                meta = ObjectMetadata(**data)
                self._cache[meta.object_id] = meta
            except Exception:
                logger.exception("Falha ao ler metadado: %s", meta_file)

    def _lock_for(self, object_id: str) -> asyncio.Lock:
        return self._locks[object_id]

    def get_capacity_bytes(self) -> int:
        if self._capacity_bytes is not None:
            return self._capacity_bytes
        try:
            return shutil.disk_usage(self.data_dir).total
        except OSError:
            return 10 * 1024 * 1024 * 1024  # Fallback de 10 GB se disk_usage falhar

    def get_used_bytes(self) -> int:
        return sum(meta.size_bytes for meta in self._cache.values())

    def get_max_allowed_bytes(self) -> int:
        return self.get_capacity_bytes() * self.max_disk_usage_percent // 100

    def get_free_bytes(self) -> int:
        return max(0, self.get_max_allowed_bytes() - self.get_used_bytes())

    def list_objects(self) -> list[ObjectMetadata]:
        return list(self._cache.values())

    def get_metadata(self, object_id: str) -> ObjectMetadata:
        validate_object_id_str(object_id)
        if object_id not in self._cache:
            meta_path = self.meta_dir / f"{object_id}.json"
            if not meta_path.exists():
                raise ObjectNotFoundError(f"Objeto não encontrado: {object_id}")
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            self._cache[object_id] = ObjectMetadata(**data)
        return self._cache[object_id]

    def _save_metadata(self, meta: ObjectMetadata) -> None:
        tmp_meta = self.tmp_dir / f"{meta.object_id}_{uuid.uuid4().hex}.meta.tmp"
        tmp_meta.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        target_meta = self.meta_dir / f"{meta.object_id}.json"
        tmp_meta.replace(target_meta)
        self._cache[meta.object_id] = meta

    async def put(
        self,
        object_id: str,
        stream: AsyncIterable[bytes],
        expected_sha256: str | None = None,
        content_length: int | None = None,
    ) -> ObjectMetadata:
        validate_object_id_str(object_id)

        async with self._lock_for(object_id):
            previous_meta = self._cache.get(object_id)
            previous_size = previous_meta.size_bytes if previous_meta else 0

            # Validação prévia de capacidade se o tamanho for informado
            if content_length is not None:
                needed = content_length - previous_size
                if needed > self.get_free_bytes():
                    raise InsufficientStorageError(
                        f"Capacidade do nó excedida: requerido {needed}B, disponível {self.get_free_bytes()}B"
                    )

            tmp_path = self.tmp_dir / f"{object_id}_{uuid.uuid4().hex}.tmp"
            hasher = hashlib.sha256()
            bytes_written = 0

            try:
                with open(tmp_path, "wb") as f:
                    async for chunk in stream:
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        needed = bytes_written - previous_size
                        if needed > self.get_free_bytes():
                            raise InsufficientStorageError("Capacidade do nó excedida durante gravação")
                        hasher.update(chunk)
                        f.write(chunk)

                computed_hash = hasher.hexdigest().lower()

                if expected_sha256 is not None:
                    expected_normalized = expected_sha256.strip().lower()
                    if computed_hash != expected_normalized:
                        raise HashMismatchError(
                            f"Hash SHA-256 diverge: esperado '{expected_normalized}', calculado '{computed_hash}'"
                        )

                # Persistência atômica: substituição do arquivo definitivo
                target_path = self.objects_dir / object_id
                tmp_path.replace(target_path)

                now_str = datetime.now(timezone.utc).isoformat()
                meta = ObjectMetadata(
                    object_id=object_id,
                    size_bytes=bytes_written,
                    sha256=computed_hash,
                    created_at=now_str,
                    last_scrubbed_at=None,
                    status="HEALTHY",
                )
                self._save_metadata(meta)
                logger.info(
                    "put_object_success node=%s object_id=%s size=%d sha256=%s",
                    self.node_id,
                    object_id,
                    bytes_written,
                    computed_hash,
                )
                return meta

            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise

    async def get_stream(self, object_id: str, verify: bool = True) -> tuple[ObjectMetadata, AsyncIterator[bytes]]:
        validate_object_id_str(object_id)

        meta = self.get_metadata(object_id)
        object_path = self.objects_dir / object_id

        if not object_path.exists():
            raise ObjectNotFoundError(f"Arquivo físico do objeto não existe: {object_id}")

        if verify:
            # Verificação prévia de integridade antes do envio
            hasher = hashlib.sha256()
            with open(object_path, "rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            current_hash = hasher.hexdigest().lower()

            if current_hash != meta.sha256.lower():
                meta.status = "CORRUPTED"
                self._save_metadata(meta)
                logger.error(
                    "read_corruption_detected node=%s object_id=%s expected=%s got=%s",
                    self.node_id,
                    object_id,
                    meta.sha256,
                    current_hash,
                )
                raise CorruptedObjectError(
                    f"Objeto {object_id} corrompido: hash em disco ({current_hash}) diverge do metadado ({meta.sha256})"
                )

        async def file_chunks() -> AsyncIterator[bytes]:
            with open(object_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return meta, file_chunks()

    async def delete(self, object_id: str) -> bool:
        validate_object_id_str(object_id)

        async with self._lock_for(object_id):
            object_path = self.objects_dir / object_id
            meta_path = self.meta_dir / f"{object_id}.json"

            if not object_path.exists() and not meta_path.exists() and object_id not in self._cache:
                raise ObjectNotFoundError(f"Objeto não encontrado para exclusão: {object_id}")

            if object_path.exists():
                object_path.unlink(missing_ok=True)
            if meta_path.exists():
                meta_path.unlink(missing_ok=True)

            self._cache.pop(object_id, None)
            logger.info("delete_object_success node=%s object_id=%s", self.node_id, object_id)
            return True

    async def scrub(self, specific_object_id: str | None = None) -> ScrubReport:
        """Executa verificação periódica de integridade (scrubbing) sobre os objetos."""
        start_time = time.monotonic()
        targets = [specific_object_id] if specific_object_id else list(self._cache.keys())

        healthy_count = 0
        corrupted_count = 0
        corrupted_ids: list[str] = []

        now_str = datetime.now(timezone.utc).isoformat()

        for obj_id in targets:
            async with self._lock_for(obj_id):
                meta = self._cache.get(obj_id)
                if meta is None:
                    continue

                obj_path = self.objects_dir / obj_id
                if not obj_path.exists():
                    meta.status = "CORRUPTED"
                    meta.last_scrubbed_at = now_str
                    self._save_metadata(meta)
                    corrupted_count += 1
                    corrupted_ids.append(obj_id)
                    continue

                hasher = hashlib.sha256()
                with open(obj_path, "rb") as f:
                    while chunk := f.read(65536):
                        hasher.update(chunk)
                current_hash = hasher.hexdigest().lower()

                meta.last_scrubbed_at = now_str
                if current_hash == meta.sha256.lower():
                    meta.status = "HEALTHY"
                    healthy_count += 1
                else:
                    meta.status = "CORRUPTED"
                    corrupted_count += 1
                    corrupted_ids.append(obj_id)
                    logger.error(
                        "scrub_corruption_detected node=%s object_id=%s expected=%s got=%s",
                        self.node_id,
                        obj_id,
                        meta.sha256,
                        current_hash,
                    )
                self._save_metadata(meta)

        duration = time.monotonic() - start_time
        report = ScrubReport(
            node_id=self.node_id,
            disk_id=self.disk_id,
            total_objects=len(self._cache),
            scanned_objects=len(targets),
            healthy_objects=healthy_count,
            corrupted_objects=corrupted_count,
            corrupted_ids=corrupted_ids,
            duration_seconds=round(duration, 4),
        )
        self.last_scrub_report = report
        logger.info(
            "scrub_completed node=%s scanned=%d healthy=%d corrupted=%d duration=%.4fs",
            self.node_id,
            report.scanned_objects,
            report.healthy_objects,
            report.corrupted_objects,
            duration,
        )
        return report

    def corrupt_for_testing(self, object_id: str) -> None:
        """Injeta corrupção física alterando um byte do arquivo em disco (apenas para testes)."""
        validate_object_id_str(object_id)
        obj_path = self.objects_dir / object_id
        if not obj_path.exists():
            raise ObjectNotFoundError(f"Objeto não existe para corrupção: {object_id}")

        size = obj_path.stat().st_size
        if size == 0:
            with open(obj_path, "wb") as f:
                f.write(b"X")
        else:
            with open(obj_path, "r+b") as f:
                byte = f.read(1)
                corrupted_byte = bytes([(byte[0] ^ 0xFF)])
                f.seek(0)
                f.write(corrupted_byte)
        logger.warning("corruption_injected_for_testing node=%s object_id=%s", self.node_id, object_id)
