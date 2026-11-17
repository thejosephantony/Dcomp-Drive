# DComp Drive — Sistemas Distribuídos (UFS)

Sistema distribuído de armazenamento de arquivos, desenvolvido para a **Parte 1** da atividade do Prof. Rafael Oliveira Vasconcelos (entrega: **18/11/2026**). Contém controlador FastAPI, PostgreSQL persistente, cinco nós de armazenamento Docker, replicação N configurável, verificação SHA-256, scrubbing, re-replicação automática, gerenciamento de capacidade, CLI e painel web opcional.

> **Estado de validação em 17/11/2026:** **64 testes aprovados, 1 aviso**, na EC2. Confirmados na AWS: CLI direta com SHA-256, RF5 (falha e reparo), RF6 (retorno e descarte de cópias obsoletas), RF7 (drain), RF8 (concorrência, PASS), RF9 (AES-256-GCM em disco e compatibilidade), RF10 (persistência), RF11 (estado/métricas) e RF12 (logs JSON). RF3 com N=2 e limites de destinos/capacidade passaram em ensaio real (PASS, HTTP 507 nos casos impossíveis). Cinco storages são contêineres no mesmo host físico. Não confundir o branch em revisão com a versão integrada em main.

## 1. Arquitetura

~~~text
CLI direta (contêiner "client")
  ├── controle: REST /files/prepare, /files/metadata, /commit e /status
  │      ↓
  │   FastAPI controller ─── PostgreSQL (metadados)
  │      ↑       ↕ heartbeat/registro/repair
  └── bytes ───> storage-1 | storage-2 | storage-3 | storage-4 | storage-5
                    ↳ volumes Docker separados + AES-GCM opcional

Navegador (bônus): interface web -> gateway do controlador -> storages.
~~~

**Plano de controle:** nome, diretório, versões, SHA-256, estado das réplicas, nós, discos, ACKs e orquestração de reparos. **Plano de dados da CLI direta:** transferências byte a byte ocorrem entre cliente e Storage Nodes; o controlador não encaminha o arquivo. O gateway da interface web permanece disponível como modo de conveniência e **não** corresponde ao caminho de dados direto. Cada réplica ocupa nó/disco/máquina lógica distinta.

Cada serviço de armazenamento é um **contêiner na mesma EC2**, com volume próprio. Isso simula cinco máquinas lógicas, mas não garante tolerância à perda do host físico.

## 2. Instalação e inicialização

Requisitos: Docker Engine, Docker Compose v2 e acesso ao Git. No diretório do projeto:

~~~bash
git clone https://github.com/thejosephantony/Dcomp-Drive.git
cd Dcomp-Drive
# Enquanto os PRs empilhados não forem integrados à main:
git fetch origin
git switch --detach origin/feature/parte1-rf9-direct-client

cp .env.example .env
# Edite .env e defina POSTGRES_PASSWORD com uma senha privada.
mkdir -p demo-files
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/status
curl -fsS http://127.0.0.1:8000/metrics
~~~

**Não execute `docker compose down -v`**: a opção `-v` apaga o PostgreSQL e os volumes dos objetos. `docker compose stop` ou `docker compose down` sem `-v` preservam os volumes.

O controller publica a porta **apenas em 127.0.0.1:8000**, e os storages comunicam-se na rede Docker privada. Para a interface web remota, abra um túnel SSH até a EC2 e navegue para `http://127.0.0.1:8000/`. Não exponha essas APIs diretamente à internet; ainda não há autenticação de usuários.

## 3. Cliente direto (principal, exigência de separação de planos)

O serviço `client` usa um *profile* opcional. Cada comando abaixo cria um contêiner temporário na rede Docker, com acesso a `/work` (pasta `demo-files/` do host). Ele usa `DCOMP_BACKEND=http` e `DCOMP_DIRECT_STORAGE=true` automaticamente.

~~~bash
printf 'Documento DComp Drive\n' > demo-files/origem.txt

# Uma sequência de uso completa em dados descartáveis:
docker compose run --rm client mkdir /documentos
docker compose run --rm client put /work/origem.txt /documentos/origem.txt
docker compose run --rm client ls /
docker compose run --rm client ls /documentos
docker compose run --rm client stat /documentos/origem.txt
docker compose run --rm client get /documentos/origem.txt /work/baixado.txt
sha256sum demo-files/origem.txt demo-files/baixado.txt
docker compose run --rm client mv /documentos/origem.txt /documentos/novo.txt
docker compose run --rm client stat /documentos/novo.txt
docker compose run --rm client rm /documentos/novo.txt
docker compose run --rm client rmdir /documentos
docker compose run --rm client status
~~~

Se `/documentos` já existir, `mkdir` retorna erro de conflito; use outro nome para o ensaio.

**Drain (RF7):** execute em um laboratório com ao menos N outros nós e capacidade livre:

~~~bash
docker compose run --rm client drain storage-1
curl -fsS http://127.0.0.1:8000/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics | python3 -m json.tool
# Só depois da migração, opcionalmente devolver o nó à seleção de destinos:
curl -fsS -X POST http://127.0.0.1:8000/nodes/storage-1/undrain
~~~

Isso coloca `storage-1` em `DRAINING`; o reparador migra cópias com validação de hash, sem novas alocações nesse nó. Aguarde todas as réplicas voltarem a N antes de removê-lo. **Não faça drain em uma demonstração que dependa do mesmo conjunto de cinco nós sem assegurar capacidade sobrando.**

### CLI com túnel Windows

O backend HTTP por padrão continua aceitando `put/get` via gateway em `DCOMP_DIRECT_STORAGE=false` para compatibilidade com computadores que não resolvem os nomes privados dos storages. Para demonstrar a arquitetura exigida, use a CLI no serviço Docker `client`, que **fala diretamente** com os Storage Nodes.

## 4. REST e observabilidade

| Método e rota | Finalidade |
| --- | --- |
| `POST /files/prepare` | Reservar N destinos; retorna `write_id` |
| `POST /files/uploads/{id}/ack` | ACK do nó após gravação |
| `POST /files/uploads/{id}/commit` | Publicar versão após W = N ACKs |
| `GET /files/metadata?path=/...` | Hash, tamanho, data, versão, nós, discos e estado |
| `GET /files/entries?path=/` | Listar diretórios/arquivos |
| `POST /files/directories` | Criar diretório (`{"path":"/x"}`) |
| `DELETE /files/directories?path=/x` | Remover diretório vazio |
| `POST /files/move` | Renomear arquivo (`{"src":"/x","dst":"/y"}`) |
| `DELETE /files/entry?path=/x` | Excluir arquivo |
| `POST /nodes/{id}/drain` | Migrar cópias antes de retirar nó |
| `POST /nodes/{id}/reconcile` | Reconciliar inventário de nó retornando |
| `GET /status` | Nós/discos, arquivos por estado, reparos ativos |
| `GET /metrics` | Requisições, latência média, bytes e estados de réplicas |
| `PUT /files/content`, `GET /files/content` | Gateway legado da interface web |
| `POST http://storage-N:9000/scrub` | Verificação física explícita |

Os Storages são serviços privados. O scrubbing periódico é controlado por `SCRUB_INTERVAL_SECONDS` (padrão 15 s). Logs são emitidos em JSON por `LOG_FORMAT=json` (timestamp UTC, componente, evento, identificadores).

## 5. Configurações e criptografia (RF9)

O arquivo `.env.example` documenta opções. Valores operacionais principais: `REPLICATION_FACTOR=3`, `NODE_TIMEOUT_SECONDS=6`, `MAX_DISK_USAGE_PERCENT=90`, `MAX_CONCURRENT_RECOVERIES=1`, `REPAIR_BANDWIDTH_BYTES_PER_SEC=0` (sem limite por reparo) e `SCRUB_INTERVAL_SECONDS=15`.

Por padrão, **dados não cifrados**: `ENCRYPTION_ENABLED=false`. Para habilitar AES-256-GCM real, gere **localmente** uma chave de 32 bytes e guarde somente no `.env` privado (nunca em Git):

~~~bash
python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
# Copie o resultado para .env:
# ENCRYPTION_ENABLED=true
# ENCRYPTION_KEY_BASE64=<valor gerado>
docker compose up -d --build
~~~

A cifra ocorre **nos Storage Nodes** antes da persistência; arquivos cifrados têm nonce aleatório e tag GCM no disco. O **SHA-256 é sobre o texto claro**; leitura e scrubbing de dados cifrados validam o conteúdo decifrado e a tag de autenticação. Metadados registram o modo por objeto; arquivos legados não cifrados continuam legíveis. **Não perca ou troque a chave**: ela é indispensável para ler objetos cifrados. A chave é uma variável de ambiente local de laboratório; não há KMS, rotação automática nem autenticação dos serviços.

## 6. Concorrência, consistência e falhas

- Semântica de escrita: versões imutáveis com estado `PENDING` e publicação do ponteiro atual somente após `COMMITTED`; **W = N**.
- Leituras usam versão publicada e verificam SHA-256; falhas físicas disparam tentativa de outra réplica.
- PostgreSQL mantém diretórios, versões, réplicas, capacidades e inventário após reinicializações.
- Heartbeats marcam nós `SUSPECT` e `DEAD`; reparos priorizam arquivos com menos réplicas vivas, respeitam destinos distintos, capacidade e limite configurável.
- Um nó que retorna registra inventário e o controlador verifica hashes antes de reaproveitar réplicas `LOST`; versões obsoletas conhecidas são descartadas.
- A consistência é forte para a **publicação de metadados** de uma versão; a cópia física distribuída não participa da transação do PostgreSQL, portanto falhas e concorrência podem criar órfãos ou limpeza pendente. Em termos de CAP, durante partições que impossibilitam W=N prioriza-se consistência de escrita sobre disponibilidade.
- `mv` de diretórios não é suportado; `rm` faz limpeza física em melhor esforço e não oferece fila persistente de coleta.
- O caminho direto ainda carrega o arquivo em memória no cliente; não há chunking por objeto nem suporte testado a volumes muito grandes. O limite do gateway web é 32 MiB; a CLI direta não usa esse gateway.
- Limite de vazão de reparos aplica-se **por transferência**, não à taxa agregada entre vários reparos concorrentes.
- Sem autenticação/autorizações por usuário, TLS interno, controladores redundantes ou domínios de falha físicos.

## 7. Testes e demonstração da Parte 1

~~~bash
docker compose exec -T controller python -m pytest -q tests
docker compose logs --tail=120 controller storage-1
~~~

Evidências anteriores de RF1/RF4 na AWS encontram-se em `docs/evidencias/parte1/`. Os documentos `docs/rf1-namespace.md` e `docs/rf4-corruption-demo.md` descrevem a injeção segura de uma única falha em um arquivo descartável e a verificação de hashes.

**Roteiro para apresentação de até 5 minutos:** (1) mostrar N=3 após `put`, (2) download e hash, (3) desligar temporariamente um storage e observar `UNDER_REPLICATED → RECOVERING → HEALTHY`, (4) injetar corrupção em uma réplica de arquivo **descartável** e mostrar detecção e nova cópia. Nunca corromper dados de colegas ou desativar o PostgreSQL.

## 8. Colaboração e histórico

- **Joseph Antony:** arquitetura, controlador, PostgreSQL, integração, AWS, interface web e documentação.
- **Thayla Figueiredo:** nós de armazenamento, persistência física, integridade e capacidade.
- **Jeferson Gonzaga:** CLI, comunicação, recuperação e testes.

Os commits devem preservar a autoria real de cada membro. Os PRs da equipe estão empilhados e devem ser integrados em ordem depois de revisão; não atribua autoria de componentes gerados sem revisão correspondente. As limitações devem aparecer honestamente na apresentação e no relatório técnico.

**Referência:** enunciado oficial "Atividade Prática: Sistema Distribuído de Armazenamento de Arquivos" (UFS, Sistemas Distribuídos, Parte 1).

## 9. Entregáveis da Parte 1

- **Relatório técnico de seis páginas (PDF):** disponível como artefato produzido para a equipe revisar e entregar; fonte editável em [docs/relatorio-tecnico-parte1.md](docs/relatorio-tecnico-parte1.md).
- **Roteiro de vídeo ou apresentação (até cinco minutos):** [docs/roteiro-demonstracao-parte1.md](docs/roteiro-demonstracao-parte1.md).
- **Validação de N configurável (RF3) e limite de capacidade (RF7), <b>PASS na AWS</b>:** `sudo docker compose exec -T controller python scripts/test_rf3_allocation_live.py` (cria somente `/rf3-n2-demo.bin`, valida N=2 com máquinas/discos lógicos distintos e espera HTTP 507 para N=6 e arquivo sem capacidade).
- **Evidências integradas na EC2 (17/11/2026):** [docs/evidencias/parte1/aws-validacao-integrada-2026-11-17.md](docs/evidencias/parte1/aws-validacao-integrada-2026-11-17.md).
- **Teste de concorrência ao vivo (RF8):** `sudo docker compose exec -T controller python scripts/test_concurrency_live.py` (cria apenas `/rf8-concorrencia-demo.bin` e imprime resultados JSON).
- **Evidências de RF1/RF4:** [docs/evidencias/parte1](docs/evidencias/parte1).

O relatório não deve declarar as funcionalidades novas como validadas antes da execução de CI, ensaios na EC2 e revisão da equipe.
