"""Configuração do cliente, lida de variáveis de ambiente (ver .env.example)."""

import os
from pathlib import Path

# Fator de replicação (N) e algoritmo de hash devem bater com o controlador real
# quando ele existir; hoje só afetam o comportamento do MockBackend.
REPLICATION_FACTOR = int(os.environ.get("REPLICATION_FACTOR", "3"))
HASH_ALGORITHM = os.environ.get("HASH_ALGORITHM", "sha256")

# Quantidade de nós de armazenamento simulados pelo mock (equivalente aos
# "storage-1".."storage-5" do docker-compose real).
MOCK_NODE_COUNT = int(os.environ.get("MOCK_NODE_COUNT", "5"))

# Onde o MockBackend persiste metadados e bytes entre chamadas de CLI.
# Cada invocação da CLI é um processo novo, então sem isso o estado se perderia
# a cada comando (ex.: dar "put" e depois "get" em comandos separados).
MOCK_STATE_DIR = Path(os.environ.get("DCOMP_MOCK_DIR", ".dcomp_mock"))
