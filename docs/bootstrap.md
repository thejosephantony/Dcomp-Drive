# Bootstrap: controlador, PostgreSQL e descoberta dos nós

Este guia descreve **somente** o bootstrap técnico da API FastAPI, banco PostgreSQL e
cinco processos de armazenamento com registro e heartbeat. Não confundir esse
resultado com a implementação da replicação e recuperação dos arquivos.

## Componentes e arquivos

| Componente | Implementação inicial |
| --- | --- |
| Controlador | controller/main.py — saúde, registro, heartbeat e estado do cluster |
| Metadados | controller/models.py — tabelas nodes e disks |
| Migrações | migrations/versions/0001_nodes_and_disks.py — Alembic |
| Monitor | controller/monitor.py — detecção de SUSPECT/DEAD |
| Nós | storage/main.py — registro automático, heartbeat e /health |
| Infraestrutura | docker-compose.yml — PostgreSQL + controlador + cinco nós |

## Pré-requisitos

- Docker com plugin Compose;
- porta 8000 disponível;
- Git para clonar o repositório.

## Primeiro uso

~~~bash
cp .env.example .env
# Defina uma senha local em .env (não versionar o arquivo).
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/status
~~~

Após alguns segundos, /status deverá listar cinco nós com estado HEALTHY.
Se o PostgreSQL ou o controlador não estiverem prontos no primeiro momento,
os nós continuam tentando registrar-se em segundo plano.

### Testar detecção de falha

~~~bash
docker compose stop storage-3
# Aguarde mais que NODE_TIMEOUT_SECONDS
curl http://localhost:8000/status
docker compose start storage-3
# Após o registro/heartbeat:
curl http://localhost:8000/status
~~~

O nó deve transitar para SUSPECT e DEAD após ausência de heartbeat,
voltando a HEALTHY quando seu processo retoma a comunicação.

**Nota:** esta demonstração detecta a falha do processo. A re-replicação
de dados **ainda não existe**.

### Confirmar persistência

~~~bash
docker compose restart controller
curl http://localhost:8000/status
~~~

Metadados e registro dos nós permanecem no volume PostgreSQL e os nós
continuam enviando heartbeats. Não execute docker compose down -v se
quiser preservar os dados do banco.

### Inspecionar migrações

~~~bash
docker compose exec controller alembic current
docker compose exec controller alembic history
~~~

## Endpoints implementados

| Método | Endpoint | Resultado |
| --- | --- | --- |
| GET | /health | Confirma acesso do controlador ao banco |
| POST | /nodes/register | Registra/atualiza um nó e seu(s) disco(s) |
| POST | /nodes/{node_id}/heartbeat | Atualiza capacidade e último heartbeat |
| GET | /status | Lista nós, capacidade e contagem por estado |
| GET | /health no storage | Confirma processo de armazenamento ativo |

Exemplo de registro (o storage faz isso automaticamente):

~~~json
{
  "node_id": "storage-1",
  "machine_id": "storage-1",
  "address": "http://storage-1:9000",
  "disks": [
    {"disk_id": "storage-1-disk0", "capacity_bytes": 100000, "used_bytes": 100}
  ]
}
~~~

Os endpoints propostos no README para prepare, commit, get, put,
scrubbing e recuperação **não estão disponíveis** nesta fase.

## Limitações conhecidas

- Os cinco volumes Docker podem compartilhar o **mesmo disco físico**;
  o experimento simula nós distintos, mas não comprova tolerância a
  falhas físicas independentes.
- A capacidade obtida do sistema de arquivos representa o volume/host
  visível ao contêiner; o contabilizador de espaço por objeto deve ser
  refinado na implementação das transferências.
- O controlador e o PostgreSQL são instâncias únicas.
- As chamadas internas ainda não implementam autenticação nem TLS;
  restrinja a execução a um ambiente de desenvolvimento confiável.
- A detecção por heartbeat não significa recuperação de dados:
  a lógica de re-replicação será uma etapa separada.

## Próximos passos para a equipe

1. **Joseph:** expandir o esquema com arquivos, versões, réplicas e operações
   pendentes, além dos endpoints /files/prepare e /files/commit.
2. **Thayla:** criar operações reais dos objetos nos storage nodes,
   SHA-256 em escrita/leitura, scrubbing e cópia entre nós.
3. **Jeferson:** implementar HttpBackend para o cliente, integrar as
   operações reais e automatizar testes de falhas e carga.

Cada mudança deve incluir testes, commits identificáveis e revisão por PR.
