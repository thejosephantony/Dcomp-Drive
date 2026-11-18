#!/usr/bin/env python3
"""Gera os segredos exclusivos da stack pública, sem exibi-los no terminal."""
import base64
import os
from pathlib import Path
import secrets
import sys

target = Path(__file__).resolve().parents[1] / ".env.public"
if target.exists():
    print("ERRO: .env.public já existe; não sobrescrever chaves nem senha do banco.")
    sys.exit(1)

content = "\n".join([
    "# Segredos internos da stack isolada. Não enviar ao GitHub nem ao ChatGPT.",
    "POSTGRES_PASSWORD=" + secrets.token_hex(24),
    "DEMO_ADMIN_TOKEN=" + secrets.token_urlsafe(48),
    "ENCRYPTION_ENABLED=true",
    "ENCRYPTION_KEY_BASE64=" + base64.b64encode(secrets.token_bytes(32)).decode(),
    "PUBLIC_MAX_TRANSFER_BYTES=1048576",
    "PUBLIC_STORAGE_CAPACITY_BYTES=33554432",
    "PUBLIC_DEMO_MAX_FILES=100",
    "SCRUB_INTERVAL_SECONDS=15",
    "",
])
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)
except BaseException:
    target.unlink(missing_ok=True)
    raise
print(f"Configuração privada criada em {target} (permissões 0600).")
