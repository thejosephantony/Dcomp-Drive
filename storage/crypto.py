"""AES-256-GCM para dados persistidos no disco dos Storage Nodes (RF9).

Formato físico: DCD1 (4 bytes) || nonce aleatório (12 bytes) ||
                texto cifrado com tag GCM de 16 bytes.
O SHA-256 do catálogo é calculado sempre sobre o texto claro.
"""
import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"DCD1"


class EncryptionUnavailableError(Exception):
    """Dados cifrados não podem ser lidos sem a chave persistente."""


class AuthenticationFailureError(Exception):
    """A validação criptográfica do objeto em disco falhou."""


class StorageCipher:
    def __init__(self):
        enabled = os.getenv("ENCRYPTION_ENABLED", "false").strip().lower()
        if enabled not in ("true", "false", "1", "0", "yes", "no"):
            raise ValueError("ENCRYPTION_ENABLED precisa ser true ou false")
        self.enabled = enabled in ("true", "1", "yes")
        key_text = os.getenv("ENCRYPTION_KEY_BASE64", "").strip()
        key = None
        if key_text:
            try:
                key = base64.b64decode(key_text, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("ENCRYPTION_KEY_BASE64 deve ser Base64 válido") from exc
            if len(key) != 32:
                raise ValueError("AES-256-GCM exige chave de 32 bytes (Base64)")
        if self.enabled and key is None:
            raise ValueError("ENCRYPTION_KEY_BASE64 obrigatório quando ENCRYPTION_ENABLED=true")
        self._key = key

    def encrypt(self, object_id: str, plain: bytes) -> bytes:
        if not self.enabled or self._key is None:
            raise EncryptionUnavailableError("Criptografia não habilitada")
        nonce = os.urandom(12)
        # ID físico participa como dado adicional autenticado (AAD).
        return MAGIC + nonce + AESGCM(self._key).encrypt(
            nonce, plain, object_id.encode("utf-8")
        )

    def decrypt(self, object_id: str, blob: bytes) -> bytes:
        if self._key is None:
            raise EncryptionUnavailableError(
                "Chave de armazenamento cifrado ausente; configure ENCRYPTION_KEY_BASE64"
            )
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 12 + 16:
            raise AuthenticationFailureError("Cabeçalho ou tag de cifra inválidos")
        nonce = blob[len(MAGIC):len(MAGIC) + 12]
        data = blob[len(MAGIC) + 12:]
        try:
            return AESGCM(self._key).decrypt(
                nonce, data, object_id.encode("utf-8")
            )
        except InvalidTag as exc:
            raise AuthenticationFailureError("Tag AES-GCM não confere") from exc
