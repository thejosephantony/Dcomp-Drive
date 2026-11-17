# DComp Drive — Relatório técnico da Parte 1

**Universidade Federal de Sergipe (UFS)** — Departamento de Computação, Sistemas Distribuídos (COMP0470).  
**Professor:** Rafael Oliveira Vasconcelos.  
**Integrantes:** Thayla Almeida Figueiredo; Joseph Antony dos Santos Leite; Jeferson Gonzaga dos Santos Filho.  
**São Cristóvão (SE), 18 de novembro de 2026.**

Esta é a versão **editável** do conteúdo técnico. O documento formatado para entrega é o PDF de **8 páginas**, denominado `DComp_Drive_Relatorio_Tecnico_Parte1_Entrega.pdf`, entregue separadamente; o arquivo Markdown não substitui o PDF.

## Resumo

O DComp Drive é um protótipo de armazenamento distribuído constituído de um controlador FastAPI, um banco PostgreSQL e cinco serviços Storage Node com volumes persistentes em Docker. O cliente CLI consulta o plano de controle para localizar as cópias e realiza transferência direta de dados com os nós. Implementa N configurável, hashes SHA-256, scrubbing, heartbeats, reparos, drain, reconciliação de nós e criptografia AES-256-GCM opcional. A execução integrada em uma EC2 aprovou 64 testes automatizados e ensaios de falha, retorno, concorrência, integridade, criptografia e N variável. Os cinco nós compartilham a mesma máquina física; a avaliação não demonstra resistência à perda da instância EC2.

**Palavras-chave:** Sistemas distribuídos; armazenamento de arquivos; replicação; integridade de dados; recuperação de falhas.

## 1. Introdução

A replicação distribui cópias entre destinos lógicos independentes, enquanto metadados associam diretórios, caminhos, versões e hashes. Para que o armazenamento permaneça consistente durante interrupções, o controlador somente publica versões confirmadas e dispara reparações quando faltam réplicas. Os experimentos de carga quantitativa permanecem para a Parte 2, com entrega em 30/11/2026.

## 2. Arquitetura do sistema

O controlador FastAPI recebe requisições REST de controle e mantém metadados no PostgreSQL. Cinco Storage Nodes recebem diretamente os bytes da CLI, verificam integridade e persistem os objetos em volumes distintos.

```text
                           /-- metadados --> PostgreSQL
Cliente CLI -- REST/JSON -- Controlador FastAPI
     |                              |
     +---- HTTP PUT/GET ----> [storage-1 ... storage-5]
                         volumes + SHA-256 + scrubbing + AES-GCM
```

**Figura 1 — Diagrama textual dos componentes.** No PDF, a figura é representada graficamente.

O plano de controle mantém namespace, versões, alocação, confirmações, heartbeat, estado e decisões de reparação; o plano de dados da CLI vai diretamente aos storages. O painel web opcional utiliza gateway do controlador para encaminhar os bytes e não representa separação estrita dos planos.

**Componentes:** controlador Python/FastAPI; PostgreSQL 17 para diretórios, versões, réplicas e inventário; cinco Storage Nodes Python/FastAPI e seus volumes; cliente CLI Python/httpx; Docker Compose para inicialização e rede privada.

## 3. Protocolos, posicionamento e consistência

**Protocolo REST/JSON:** `POST /files/prepare` recebe `path`, `size_bytes`, `sha256` (64 dígitos hexadecimais) e `replication_factor` opcional. A resposta contém `write_id`, `desired_replicas` e `targets`, com `node_id`, `machine_id`, `disk_id` e `address`. A CLI realiza `PUT /objects/{write_id}` diretamente nos destinos, informando o SHA-256. Cada nó valida o conteúdo, envia ACK com nó/disco/tamanho/hash, e o controlador só executa o commit após N confirmações. A consulta `GET /files/metadata?path=...` informa versão, tamanho, data, hash, estado e localização. `GET /objects/{version_id}` recupera a versão íntegra. O corpo de arquivo usa `application/octet-stream`.

**Política de posicionamento:** escolha de N destinos com `machine_id` distintos, sem repetição de disco, ordenados por capacidade disponível. Considera reservas PENDING/REPAIRING e um teto `MAX_DISK_USAGE_PERCENT`. Se N destinos não estiverem disponíveis, o controlador responde HTTP 507, sem publicar o arquivo. Foram confirmados N=3 e N=2, além das recusas para N=6 e carga acima do espaço disponível.

**Semântica de concorrência:** gravações criam versões `PENDING`; a versão acessível é a última `COMMITTED`. O PostgreSQL controla conflitos por bloqueios/transações. A consistência é forte para o **ponteiro de versão publicada**, com quórum de escrita **W=N**: nenhuma nova versão aparece ao leitor antes das N confirmações. No ensaio concorrente, duas escritas simultâneas receberam 201 e 409; doze leituras retornaram HTTP 200 e hashes de versões completas.

**Decisão CAP:** sob partição que impeça obter W=N, o sistema recusa publicar uma nova escrita, priorizando a consistência da versão anunciada em vez da disponibilidade de escrita. A leitura depende do controlador e de uma cópia íntegra acessível. Não há transação distribuída atômica entre SQL e armazenamento físico; falhas podem deixar cópias órfãs até reconciliação.

## 4. Integridade, criptografia e recuperação

O SHA-256 é calculado sobre o conteúdo original e validado no upload, download e scrubbing periódico. Réplicas corrompidas não contam como saudáveis e são substituídas após verificação de uma cópia de origem. Um ensaio RF4 anterior detectou corrupção, recompôs N=3 e preservou o hash original.

**AES-256-GCM:** a cifra ocorre no Storage Node antes da persistência. Cada objeto cifrado utiliza nonce aleatório de 96 bits, tag GCM de 128 bits e `object_id` como dado adicional autenticado. A chave de 256 bits é guardada em `ENCRYPTION_KEY_BASE64` no `.env` privado. O hash permanece sobre o texto claro; metadados locais indicam se o objeto é `AES-256-GCM` ou legado sem cifra. Na AWS, a inspeção encontrou `DCD1` no disco, sem o trecho original em texto claro; o download foi idêntico ao original. Não há KMS ou rotação automática; a chave deve ser preservada fora da EC2, em armazenamento seguro.

**Falhas:** heartbeats classificam nós como HEALTHY, SUSPECT e DEAD. A re-replicação prioriza versões com menos réplicas vivas e respeita quantidade máxima de reparos e banda por transferência. O modo DRAINING impede novos posicionamentos e permite a migração das cópias. Ao retornar, o nó envia inventário; o controlador valida a integridade e descarta cópias obsoletas.

## 5. Implementação e resultados na AWS

**Ambiente:** Ubuntu em uma EC2, um controlador, PostgreSQL e cinco storages em contêineres. Portas de banco e storages restritas à rede Docker; controlador publicado em 127.0.0.1. A suíte `pytest -q tests` resultou em **64 passed, 1 warning in 7.50s** no ensaio final informado.

| Ensaio | Resultado observado |
| --- | --- |
| Transferência direta N=3 | 46 bytes em três nós; SHA-256 e comparação binária idênticos |
| RF7 — Drain | storage-1 em DRAINING; réplicas migradas; seis arquivos HEALTHY; undrain |
| RF5 — Falha | storage-2 passou HEALTHY → SUSPECT → DEAD; reparação automática |
| RF6 — Retorno | node_reconciled com restored=0, discarded=5, skipped=0 |
| RF8 — Concorrência | Uma escrita HTTP 201, uma 409; 12/12 leituras com hashes válidos; PASS |
| RF9 — Criptografia | AES-256-GCM em três storages, plaintext ausente no disco, download idêntico; legado legível |
| RF3/RF7 — N e capacidade | N=2 publicado em storage-4/storage-3, SHA verificado; N=6 e capacidade insuficiente deram 507 |
| RF10 — Persistência | Cinco arquivos preservados após reinício e backups do banco e cinco storages verificados |
| RF11/RF12 | /status, /metrics e logs JSON demonstrados |

As evidências detalhadas e os comandos encontram-se em `docs/evidencias/parte1/aws-validacao-integrada-2026-11-17.md` e `scripts/`.

## 6. Conformidade, limitações e dimensionamento

Os RF1 a RF12 possuem implementação e testes compatíveis com o protótipo; os ensaios acima registram o escopo efetivamente observado. O RF4 foi ensaiado anteriormente; a demonstração obrigatória de corrupção ainda precisa constar no vídeo ou ocorrer presencialmente. A entrada de um sexto nó, sem reinício do controlador, ainda não foi demonstrada em teste específico.

Todos os storages residem numa única EC2: não há tolerância à perda desse host físico nem alta disponibilidade do controlador/PostgreSQL. CLI e reparador mantêm arquivos inteiros em memória; não há chunking por objeto para cargas muito grandes. O painel web ainda usa gateway do controlador, e os contadores de tráfego do gateway não incluem bytes enviados diretamente pela CLI. Sem autenticação individual, TLS interno, KMS e rotação de chaves. A remoção de arquivos não dispõe de fila durável de limpeza física; `mv` não move diretórios.

O laboratório validou cinco nós e uma quantidade pequena de arquivos. **A Parte 2 poderá avaliar oito nós e mil arquivos pequenos como cenários planejados**, sem que isso represente capacidade já medida ou garantida.

### 6.1 Divisão de tarefas

- **Thayla Almeida Figueiredo:** implementação dos Storage Nodes, persistência física, SHA-256, scrubbing e capacidade.
- **Joseph Antony dos Santos Leite:** arquitetura, controlador e metadados, PostgreSQL, implantação na AWS, interface web e integração.
- **Jeferson Gonzaga dos Santos Filho:** CLI, protocolos de comunicação, recuperação de falhas e testes.

A equipe deve validar a correspondência dessa divisão com a autoria real no histórico Git antes da submissão.

## 7. Execução e reprodução

```bash
cp .env.example .env            # definir POSTGRES_PASSWORD em segredo
mkdir -p demo-files
docker compose config --quiet
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
docker compose exec -T controller python -m pytest -q tests
docker compose exec -T controller python scripts/test_concurrency_live.py
docker compose exec -T controller python scripts/test_rf3_allocation_live.py
docker compose run --rm client status
```

Os serviços demoram a ficar prontos após a migração do banco: aguardar o `/health` antes dos scripts. Não executar `docker compose down -v` com dados a preservar; não publicar chaves ou banco na internet.

## 8. Considerações finais

A validação demonstra publicação de versões com N confirmações, leitura íntegra, recuperação após falha de storage, reconciliação, drain e criptografia autenticada, nos limites da simulação. A Parte 2 avaliará desempenho e escalabilidade quantitativos. A entrega da Parte 1 inclui PDF de 8 páginas e vídeo de até 5 minutos ou apresentação presencial, contendo gravação replicada, leitura, falha com recuperação e corrupção detectada.

## Referências

UNIVERSIDADE FEDERAL DE SERGIPE. **Atividade prática: sistema distribuído de armazenamento de arquivos**. Enunciado da disciplina Sistemas Distribuídos — COMP0470. São Cristóvão, 2026.

EQUIPE DCOMP DRIVE. **DComp Drive: código-fonte, documentação e testes**. Repositório GitHub, 2026. Disponível em: https://github.com/thejosephantony/Dcomp-Drive.
