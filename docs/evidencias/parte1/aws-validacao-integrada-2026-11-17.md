# DComp Drive — evidências integradas na AWS em 17/11/2026

**Ambiente:** EC2 Ubuntu (um host físico), Docker Compose (controlador FastAPI, PostgreSQL e cinco Storage Nodes com volumes distintos). **Versão executada nos ensaios:** `14a6976` em `feature/parte1-rf9-direct-client`. Horários de eventos em UTC. **Não publicar** `.env` ou `ENCRYPTION_KEY_BASE64`.

## Suíte e persistência
- `sudo docker compose exec -T controller python -m pytest -q tests` → **64 passed, 1 warning in 7.36s**. Aviso de depreciação em dependência Starlette/httpx.
- Após religar a EC2, `/health` respondeu `{"status":"ok"}`; contêineres PostgreSQL, controlador e cinco storages iniciaram. Cinco arquivos anteriores permaneceram `HEALTHY` com N=3.
- Backup pré-ensaios em `~/dcomp-backup-20261117-200157/`: `postgres.sql` (13 KiB) e `storage-1.tar.gz` a `storage-5.tar.gz`. Todos os arquivos tar passaram em `tar -tzf`. Backup de `.env` com chave habilitada criado separadamente, modo 0600.

## Transferência direta, RF1/RF3 e integridade
- `docker compose run --rm client put /work/direto-p1.txt /validacao-p1/direto-p1.txt` → versão 1, **46 bytes**, status `HEALTHY`, três réplicas em storage-1/2/3.
- `get` retornou o arquivo; `sha256sum` original e recuperado foram iguais a `2984f43aa700834411dbf9d0ef6d2e0efdce513ec035f8f9111726a98bfb9af9`; `cmp` confirmou bytes idênticos.
- A CLI direta usa o controlador para metadados e PUT/GET físicos com storages privados; o painel web usa gateway separado. Os cinco nós são **lógicos no mesmo host EC2** (não domínios de falha físicos).

## RF7 — drain planejado
- `docker compose run --rm client drain storage-1` → estado `DRAINING` visível em `/status`; durante migração havia arquivos `UNDER_REPLICATED` e `RECOVERING`.
- Cópia do arquivo direto foi migrada de storage-1 para storage-4; ao final seis arquivos `HEALTHY`, nenhum indisponível e storage-1 com zero bytes. `POST /nodes/storage-1/undrain` → `HEALTHY`.

## RF5/RF6 — queda, recuperação e retorno
- `docker compose stop storage-2` → monitor registrou `HEALTHY → SUSPECT → DEAD`; reparador emitiu eventos `repair_completed`; após ~20 segundos, seis arquivos `HEALTHY` com as réplicas recompletadas nos outros nós.
- `docker compose start storage-2` → `HEALTHY`; evento `node_reconciled node=storage-2 result={'restored': 0, 'discarded': 5, 'skipped': 0}`, comprovando reconciliação e descarte de cinco cópias obsoletas.
- Download após falha e retorno manteve o SHA-256 original. Prioridade/limites novos de RF5 têm testes automatizados; não foram medidos separadamente em ambiente real.

## RF9 — AES-256-GCM real
- `python -m pytest -v tests/test_encryption.py` → **2 passed**.
- Com `ENCRYPTION_ENABLED=true` e chave Base64 privada de 32 bytes, upload de `/validacao-p1/rf9-cifrado.txt` (42 bytes) publicado `HEALTHY` em três nós.
- Inspeção física em `/data/meta/<version_id>.json` → `encryption: AES-256-GCM`; arquivo em `/data/objects/<version_id>` começou com `DCD1` e não continha o texto claro em bytes.
- Download e `cmp` idênticos; arquivo anterior não cifrado também continuou legível após ativação.
- **Atenção:** preservar fora da EC2 uma cópia segura e privada da chave, sem compartilhá-la no Git.

## RF8 — concorrência
- `sudo docker compose exec -T controller python scripts/test_concurrency_live.py` → **PASS**.
- Duas gravações concorrentes no mesmo caminho: respostas HTTP **409** (conflito) e **201** (aceita).
- **12 de 12** leituras retornaram HTTP 200 com `hash_valid: true`; versão final também teve hash válido. Após ensaio, oito arquivos `HEALTHY`, nenhum `UNDER_REPLICATED` ou `UNAVAILABLE`.

## RF11/RF12 — estado, métricas e logs
- `GET /status` exibiu cinco nós/discos, estados, capacidades, arquivos por estado e reparos ativos.
- `GET /metrics` incluiu uptime, requisições, latência média, contagem de réplicas e reparos. **Os bytes de PUT/GET diretos permanecem fora dos contadores do gateway**, logo esses contadores ficaram zero no ensaio da CLI.
- Logs de storage e controlador são JSON com `timestamp`, `component`, `level`, `event`, `message` e identificadores, incluindo `repair_completed`, `node_reconciled` e `scrub_completed`.

## Pendências / limites que não devem ser declarados comprovados
1. Validar configuração **N diferente de 3** e rejeição por falta de destinos/capacidade na EC2 (há implementação e testes unitários).
2. Registrar evidência do teste de corrupção RF4 no vídeo de demonstração; houve ensaio real anterior com recuperação, mas não foi repetido no mesmo bloco integrado.
3. O experimento usa **uma EC2 e contêineres distintos**, não cinco máquinas físicas. Não demonstra sobrevivência à perda do host.
4. Contadores de tráfego do controlador não contabilizam o tráfego direto CLI↔Storage Nodes.
5. Consolidar PRs/commits na branch de entrega, atualizar relatório final/PDF e gravar vídeo de no máximo cinco minutos ou combinar demonstração ao vivo.

**Fonte da evidência:** saídas de terminal da EC2 compartilhadas pela equipe em 17/11/2026; não são resultados de CI nem promessa de implementação futura.
