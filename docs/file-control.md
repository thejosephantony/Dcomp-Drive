# Protocolo de metadados de arquivos (fase incremental)

Este PR adiciona os registros de diretórios, arquivos, versões e réplicas no
PostgreSQL, além de preparar gravações e publicar versões com um protocolo
PREPARE/ACK/COMMIT.

**Escopo:** somente plano de controle. A transferência e a verificação de
bytes nos storage nodes continuam pendentes.

## Estados

| Estado | Significado |
| --- | --- |
| PENDING | Alocação criada, versão invisível |
| COMMITTED | Todas as confirmações esperadas foram registradas |
| ABORTED | Gravação cancelada |
| EXPIRED | Gravação expirou sem confirmação |

Uma sobrescrita PENDING não substitui a última versão COMMITTED.
Os arquivos ainda não confirmados não ficam visíveis em /files/metadata.

## Endpoints

| Método | Caminho | Comportamento |
| --- | --- | --- |
| POST | /files/prepare | Registra operação pendente e seleciona N máquinas |
| POST | /files/uploads/{write_id}/ack | Registra ACK da réplica declarada |
| POST | /files/uploads/{write_id}/commit | Publica a versão após N ACKs |
| POST | /files/uploads/{write_id}/abort | Aborta operação pendente |
| GET | /files/uploads/{write_id} | Inspeciona a operação |
| GET | /files/metadata?path=/arquivo.bin | Consulta versão publicada |

Se houver menos de N máquinas elegíveis, prepare responde 507. Tentativas de
nova escrita para um mesmo arquivo pendente retornam 409.

A escolha de réplicas respeita machine_id distinto, integridade dos
identificadores dos nós, capacidade e o limite configurado de uso.
As reservas de operações pendentes são contabilizadas; não há ainda
reserva física nos nós.

## Executar

A branch depende do bootstrap do PR #4. Depois de obter a branch:

~~~powershell
git fetch origin
git switch feature/file-metadata-preparation
docker compose up --build -d
docker compose exec controller alembic current
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/status
docker compose exec controller python -m pytest -q tests
~~~

Não apague os volumes do PostgreSQL com docker compose down -v.

## Exemplo de teste de metadados em PowerShell

O teste abaixo **simula** a confirmação das réplicas. Ele valida o
protocolo do controlador, não a existência de cópias físicas dos bytes.

~~~powershell
$sha = "a" * 64
$payload = @{path="/prova.bin"; size_bytes=100; sha256=$sha; replication_factor=3} | ConvertTo-Json
$op = Invoke-RestMethod -Method Post -Uri http://localhost:8000/files/prepare -ContentType "application/json" -Body $payload
$op.targets | Format-Table node_id, machine_id, disk_id
foreach ($target in $op.targets) {
  $ack = @{node_id=$target.node_id; disk_id=$target.disk_id; sha256=$sha; size_bytes=100} | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "http://localhost:8000/files/uploads/$($op.write_id)/ack" -ContentType "application/json" -Body $ack
}
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/files/uploads/$($op.write_id)/commit"
Invoke-RestMethod -Uri "http://localhost:8000/files/metadata?path=/prova.bin"
~~~

## Limitações importantes

- A API ACK é apenas um contrato inicial **sem autenticação do nó**.
  Qualquer cliente com acesso a ela pode simular confirmações. O commit
  atual não demonstra que os arquivos existem, nem que o hash foi validado
  fisicamente nos nós. Não exponha essa interface à Internet.
- O diretório raiz existe; criação, remoção e movimentação de diretórios
  ainda serão implementadas.
- Não estão implementados PUT/GET reais, scrubbing, re-replicação,
  conciliação de falhas parciais, limpeza de réplicas antigas nem
  criptografia de dados.
- Os testes automatizados da API usam SQLite descartável; executar também
  testes com PostgreSQL para comprovar bloqueios e consistência concorrente.

## Divisão da próxima etapa

Joseph Antony cuida dos contratos, migrações e política de consistência;
Thayla Figueiredo implementa armazenamento real, hashes e cópia entre nós;
Jeferson Gonzaga integra a CLI via HTTP e automatiza testes de falhas.

O próximo avanço deve autenticar os ACKs dos nós e só confirmar uma versão
quando as réplicas físicas tiverem sido gravadas e verificadas.
