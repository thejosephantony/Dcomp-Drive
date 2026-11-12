# DComp Drive

**Sistema distribuído de armazenamento de arquivos com replicação, integridade por SHA-256 e recuperação automática de falhas.**

> **Estado do projeto:** em planejamento / implementação inicial. Este README descreve a **arquitetura-alvo**; não implica que os serviços, endpoints e comandos já estejam implementados. Atualizar as instruções de execução à medida que forem testadas.

Projeto acadêmico da disciplina de **Sistemas Distribuídos** da **Universidade Federal de Sergipe (UFS)**, sob orientação do professor **Rafael Oliveira Vasconcelos**.

**Repositório:** https://github.com/thejosephantony/Dcomp-Drive

## 1. Visão geral

O **DComp Drive** propõe um serviço de armazenamento inspirado em conceitos de HDFS, GFS, Ceph e MinIO. Em vez de depender de uma única máquina, um arquivo é armazenado em **N nós distintos**, com integridade verificada por hash. Se um nó falha ou uma réplica se corrompe, o controlador deve identificar o problema e restaurar automaticamente o número esperado de cópias, quando houver nós e capacidade suficientes.

**Exemplo (N = 3):** após o envio de um arquivo, o sistema mantém três réplicas em máquinas distintas. Se um nó fica indisponível, o arquivo permanece legível pelas cópias íntegras restantes. O controlador deve providenciar uma nova cópia em outro nó, recuperando o nível de replicação.

### Objetivos funcionais

- Operações de arquivos e diretórios: enviar, baixar, excluir, renomear/mover, listar, criar e remover diretórios e consultar metadados.
- Replicação **N configurável**, respeitando a separação entre discos e máquinas.
- Hash **SHA-256** na gravação, leitura e verificação periódica (*scrubbing*).
- Descoberta automática de nós, heartbeats, timeouts e recuperação automática.
- Gestão de capacidade, limite de utilização e retirada planejada (*drain*).
- Controle de concorrência, consistência, persistência dos metadados e prevenção de leituras parciais.
- Configuração de criptografia em repouso, logs estruturados e métricas.

## 2. Arquitetura proposta

~~~mermaid
flowchart TB
    C["Cliente CLI · Python/Typer"]
    K["Controlador · FastAPI"]
    DB[("PostgreSQL · Metadados")]
    S1["Storage 1"]
    S2["Storage 2"]
    S3["Storage 3"]
    S4["Storage 4"]
    S5["Storage 5"]

    C <-->|"Metadados e alocação"| K
    K <-->|"Transações e consultas"| DB
    C <-->|"Transferências diretas"| S1
    C <-->|"Transferências diretas"| S2
    C <-->|"Transferências diretas"| S3
    C <-->|"Transferências diretas"| S4
    C <-->|"Transferências diretas"| S5
    S1 -->|"Registro / heartbeat"| K
    S2 -->|"Registro / heartbeat"| K
    S3 -->|"Registro / heartbeat"| K
    S4 -->|"Registro / heartbeat"| K
    S5 -->|"Registro / heartbeat"| K
    K -->|"Ordens de re-replicação"| S1
    K -->|"Ordens de re-replicação"| S2
    K -->|"Ordens de re-replicação"| S3
    K -->|"Ordens de re-replicação"| S4
    K -->|"Ordens de re-replicação"| S5
    S1 <-.->|"Cópias entre nós"| S3
~~~

O **plano de controle** administra metadados, alocação, saúde e recuperação. O **plano de dados** transfere conteúdo **diretamente entre cliente e nós**, ou entre nós durante a recuperação. Os arquivos **não** devem ser transmitidos pelo controlador.

| Componente | Responsabilidade |
| --- | --- |
| Cliente CLI | Comandos, cálculo/verificação de hashes, upload e download diretamente nos nós. |
| Controlador | Alocação de réplicas, namespace, controle de concorrência, descoberta, heartbeat e recuperação. |
| PostgreSQL | Persistência dos diretórios, versões, hashes, localização das réplicas, nós e tarefas. |
| Nós de armazenamento | Gravação/leitura de dados, integridade, capacidade, heartbeat e transferência entre nós. |

**Ambiente mínimo:** 1 controlador + 5 nós de armazenamento, além de PostgreSQL com volume persistente.

## 3. Tecnologias previstas

| Camada | Tecnologia |
| --- | --- |
| Linguagem | Python 3 |
| APIs | FastAPI + HTTP/JSON |
| Banco de metadados | PostgreSQL |
| Acesso a dados | SQLAlchemy 2.x + psycopg |
| Migrações | Alembic |
| CLI | Typer |
| Integridade | hashlib / SHA-256 |
| Infraestrutura | Docker e Docker Compose |
| Testes | pytest |
| Logs | JSON estruturado |

**Por que PostgreSQL?** O banco oferece transações e controle robusto de concorrência para metadados. Entretanto, **não** distribui automaticamente o controlador nem elimina o ponto único de falha do plano de controle. A recuperação após reinicialização depende da persistência e da reconciliação com os nós.

## 4. Decisões de consistência e segurança

As decisões abaixo são iniciais e precisam ser implementadas e testadas:

| Parâmetro | Valor ou política inicial |
| --- | --- |
| Replicação | N = 3, configurável |
| Confirmação de escrita | W = N: publicar após N cópias gravadas e verificadas |
| Hash | SHA-256 sobre o conteúdo original |
| Leituras | Apenas versões confirmadas; verificar hash em cada leitura |
| Escritas concorrentes | Bloqueio/versão por arquivo; evitar *lost updates* |
| Timeout inicial | Heartbeat de 2 s, timeout de 6 s (ajustáveis) |
| Criptografia | Opção configurável; iniciar desabilitada e documentar o comportamento quando habilitada |
| Política de alocação | Nós/máquinas distintos, espaço disponível e limite de utilização |
| Recuperação | Prioridade para arquivos com menos réplicas vivas e limite de cópias simultâneas |

Com **W = N**, uma escrita pode ficar indisponível durante uma falha se não houver N destinos saudáveis. As leituras de versões já confirmadas podem continuar enquanto houver réplica íntegra **e os metadados estiverem acessíveis**. A equipe deverá justificar as escolhas de consistência e disponibilidade em relação ao teorema CAP.

**Cuidado com a atomicidade:** uma transação no PostgreSQL **não** abrange automaticamente a escrita dos arquivos nos nós remotos. O protocolo de upload deve usar estados pendentes, identificadores idempotentes e uma rotina de reconciliação para limpar ou finalizar operações interrompidas.

### Fluxo de escrita

1. O cliente calcula o SHA-256 e solicita ao controlador uma operação de escrita.
2. O controlador registra a operação como **PENDING** e seleciona N destinos válidos.
3. O cliente envia os dados diretamente aos nós indicados.
4. Cada nó valida o hash, persiste o conteúdo e confirma a operação.
5. Após o quórum configurado, o controlador publica uma versão **COMMITTED**.
6. Falhas intermediárias são tratadas por limpeza ou reconciliação; nenhuma versão incompleta pode ser lida.

### Fluxo de leitura

1. O cliente consulta o controlador para descobrir o arquivo e as réplicas íntegras.
2. Faz o download diretamente de uma réplica.
3. Verifica SHA-256; em caso de corrupção, tenta outra réplica e informa o controlador.

### Fluxo de recuperação

1. O controlador identifica um nó suspeito/morto por heartbeats e timeouts.
2. Calcula as réplicas ainda disponíveis e prioriza os arquivos mais vulneráveis.
3. Seleciona novos destinos respeitando os domínios de falha e o espaço.
4. Ordena a transferência de uma cópia saudável entre nós.
5. Confirma hash, atualiza metadados e reconcilia versões obsoletas.

## 5. Modelo relacional de metadados (proposto)

| Tabela | Conteúdo |
| --- | --- |
| **nodes** | ID, endereço, machine_id, status e último heartbeat. |
| **disks** | ID, node_id, capacidade total/utilizada e estado. |
| **directories** | Hierarquia de diretórios. |
| **files** | Caminho lógico, identificador e versão atual. |
| **file_versions** | Versão, tamanho, SHA-256, N desejado, estado e timestamps. |
| **replicas** | Versão, nó, disco, caminho interno, hash e estado. |
| **recovery_jobs** | Origem, destino, motivo, prioridade, estado e tentativas. |

Utilizar chaves estrangeiras, índices para consultas por arquivo/nó e restrições de unicidade. Impedir duplicação de réplicas na mesma máquina/disco deve ser garantido pela lógica transacional de alocação, além das restrições adequadas no banco.

## 6. Estrutura planejada

> A árvore é uma **proposta** e não uma descrição de arquivos que já existem na branch principal.

~~~text
Dcomp-Drive/
├── controller/           # FastAPI, metadados, alocação e monitoramento
├── storage/              # API dos nós, dados, hash, heartbeat e scrubbing
├── client/               # CLI e transferências
├── shared/               # Schemas e configuração compartilhada
├── migrations/           # Migrações com Alembic
├── tests/
│   ├── functional/
│   ├── failures/
│   └── performance/
├── scripts/              # Inicialização e testes reprodutíveis
├── docs/                 # Arquitetura, decisões e relatórios
├── docker-compose.yml
├── .env.example
├── requirements.txt
└── README.md
~~~

## 7. Como começar

### Clonar o repositório (disponível)

~~~bash
git clone https://github.com/thejosephantony/Dcomp-Drive.git
cd Dcomp-Drive
git status
~~~

### Executar o ambiente (a implementar)

> Os comandos seguintes **não estão validados**: dependem de criar os arquivos de infraestrutura e os serviços.

~~~bash
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f
docker compose down
~~~

O serviço PostgreSQL precisa usar **volume persistente**. Arquivos com segredos, como **.env**, não devem ser versionados; utilizar **.env.example** sem credenciais reais.

### Configuração ilustrativa

~~~dotenv
DATABASE_URL=postgresql+psycopg://dcomp:ALTERAR_SENHA@postgres:5432/dcomp_drive
REPLICATION_FACTOR=3
WRITE_QUORUM=3
HASH_ALGORITHM=sha256
HEARTBEAT_INTERVAL_SECONDS=2
NODE_TIMEOUT_SECONDS=6
MAX_CONCURRENT_RECOVERIES=2
MAX_DISK_USAGE_PERCENT=90
ENCRYPTION_ENABLED=false
~~~

Quando a criptografia for habilitada, definir algoritmo de biblioteca consolidada (por exemplo, AES-256-GCM), gerenciamento de chaves, ponto de cifragem e se os hashes se referem ao texto claro ou cifrado. **Não implementar criptografia própria.**

## 8. Interface planejada

Os comandos abaixo são **exemplos de UX**, não funcionalidades já disponibilizadas:

~~~bash
python -m client.cli put ./trabalho.pdf /documentos/trabalho.pdf
python -m client.cli get /documentos/trabalho.pdf ./download.pdf
python -m client.cli ls /documentos
python -m client.cli mkdir /documentos/projetos
python -m client.cli mv /documentos/trabalho.pdf /documentos/projetos/trabalho.pdf
python -m client.cli rm /documentos/projetos/trabalho.pdf
python -m client.cli rmdir /documentos/projetos
python -m client.cli stat /documentos/trabalho.pdf
python -m client.cli status
~~~

### Contratos HTTP preliminares

| Serviço | Método / rota | Finalidade |
| --- | --- | --- |
| Controlador | POST /nodes/register | Registro automático de nós |
| Controlador | POST /nodes/heartbeat | Saúde e capacidade |
| Controlador | POST /files/prepare | Criar operação de escrita pendente |
| Controlador | POST /files/commit | Confirmar versão após verificação |
| Controlador | GET /files/metadata | Consultar hash, estado e réplicas |
| Controlador | GET /status | Saúde e estado do cluster |
| Controlador | GET /metrics | Métricas |
| Storage | PUT /objects/{id} | Gravar objeto |
| Storage | GET /objects/{id} | Ler objeto |
| Storage | POST /objects/{id}/replicate | Criar outra réplica |
| Storage | DELETE /objects/{id} | Remover objeto |

Os contratos definitivos devem definir payloads, códigos de erro, timeouts, idempotência, concorrência e responsabilidades de cada componente. O conjunto deve ser ampliado para abranger as operações de diretórios, drain e scrubbing.

## 9. Equipe e organização

| Integrante | Área de responsabilidade |
| --- | --- |
| **Joseph Antony** | Arquitetura inicial, repositório, contratos REST, PostgreSQL, SQLAlchemy, Alembic, controlador e integração. |
| **Thayla Figueiredo** | Nós de armazenamento, persistência física, SHA-256, scrubbing e replicação de dados. |
| **Jeferson Gonzaga** | Cliente CLI, fluxos integrados, detecção de falhas, recuperação automática e testes. |

**Trabalho colaborativo:** todos devem ter commits próprios, revisar alterações, realizar testes e contribuir para a documentação.

Fluxo sugerido: criar uma branch por módulo, fazer commits pequenos, abrir Pull Requests e revisar antes de integrar à **main**.

## 10. Cronograma

| Data | Marco |
| --- | --- |
| **12/11/2026** | Arquitetura, contratos e preparação do ambiente |
| **13–14/11/2026** | Controlador, PostgreSQL, nós e operações básicas |
| **15–16/11/2026** | Replicação, hash, heartbeat e recuperação |
| **17/11/2026** | Integração, testes, README, relatório e demonstração |
| **18/11/2026** | **Entrega da Parte 1**: código, ambiente e demonstração |
| **19–29/11/2026** | Testes funcionais, falhas, desempenho, gráficos e relatório |
| **30/11/2026** | **Entrega da Parte 2**: experimentos e análise |

## 11. Testes e critérios de aceite

- [ ] Subir 1 controlador, PostgreSQL e **5 nós de armazenamento**.
- [ ] Registrar nós sem alterar configurações centrais e sem reiniciar o controlador.
- [ ] Executar operações de arquivos e diretórios pela CLI.
- [ ] Gravar N réplicas em destinos válidos e verificar SHA-256.
- [ ] Recuperar metadados após reinício do controlador.
- [ ] Detectar falhas e realizar re-replicação automática.
- [ ] Detectar corrupção na leitura e no scrubbing periódico.
- [ ] Impedir leituras de arquivos incompletos e tratar concorrência.
- [ ] Expor capacidade, estado de nós/réplicas, logs e métricas.
- [ ] Exercitar limitação de espaço, drain e retorno de nós.
- [ ] Medir latência, vazão, tempo de detecção e tempo de recuperação.
- [ ] Documentar falhas, correções, limites e evidências dos testes.

**Demonstração da Parte 1:** enviar arquivo com N = 3, mostrar as três réplicas, baixar e conferir hash, derrubar um nó, observar a recuperação automática e demonstrar detecção de corrupção em uma réplica.

**Parte 2:** identificar a versão testada por tag Git, repetir cada experimento de 3 a 5 vezes quando aplicável, calcular média e intervalo de confiança de 95%, armazenar dados brutos e analisar falhas e gargalos.

## 12. Limitações previstas

- **Controlador e PostgreSQL únicos:** podem interromper operações de metadados durante indisponibilidade, embora o estado persistido deva ser recuperável.
- **Contêineres na mesma máquina:** simulam serviços isolados, mas compartilham recursos físicos e não equivalem a cinco máquinas independentes.
- **Transação de metadados ≠ transação distribuída:** o protocolo de escrita deve reconciliar gravações remotas.
- **Desempenho:** exigirá medições para identificar gargalos de rede, disco, CPU e controlador.
- **Funcionalidades opcionais e melhorias:** não podem prejudicar os requisitos principais nem a reprodutibilidade.

## Referência acadêmica

Enunciado da atividade **Sistema Distribuído de Armazenamento de Arquivos**, disciplina **Sistemas Distribuídos** (UFS, 2026).

---

**Nota:** as seções de instalação e API serão convertidas em documentação de uso real após a implementação e validação dos módulos.