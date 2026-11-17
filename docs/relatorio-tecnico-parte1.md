# DComp Drive — Relatório Técnico da Parte 1

**Universidade Federal de Sergipe • Sistemas Distribuídos • Prof. Rafael Oliveira Vasconcelos**  
**Equipe:** Joseph Antony, Thayla Figueiredo e Jeferson Gonzaga  
**Data:** 17/11/2026 | **Entrega da Parte 1:** 18/11/2026  
**Versão:** relatório atualizado com evidências práticas de 17/11/2026; revisar paginação e exportar PDF final de 4–8 páginas antes da entrega.

## 1. Escopo e arquitetura

O DComp Drive implementa armazenamento distribuído com replicação N configurável, verificação de integridade por SHA-256, detecção de falhas e recuperação automática. O ambiente contém FastAPI (controlador e metadados), PostgreSQL persistente e cinco Storage Nodes em Docker Compose, cada um com volume próprio. A CLI opera em um serviço `client` opcional, que consulta o controlador para obter alocação e localização e transfere dados diretamente para os Storage Nodes.

```text
CLIENTE (CLI direta) ------ metadados ------> CONTROLADOR FastAPI ---> PostgreSQL
       |                                           ^  ^
       | -- bytes de arquivo diretamente -->       |  |
       +------> [Storage-1 ... Storage-5] -- ACKs/heartbeats/estado ------+
                       volumes + SHA-256 + scrubbing + AES-GCM (opcional)
```

**Separação de planos:** o controlador coordena nomes, versões, estado de nós, espaço livre, alocação e reparação. A CLI direta transmite e recebe bytes dos Storage Nodes. O dashboard web opcional mantém um gateway legado que encaminha bytes via controlador; isso permanece uma limitação assumida.

**Isolamento:** cinco máquinas *lógicas* distintas, mas no mesmo host AWS EC2. Isso simula falhas de contêiner/disco virtual, não a perda de uma máquina física inteira.

## 2. Protocolos e formatos

As chamadas de controle usam REST/JSON e os dados usam HTTP application/octet-stream.

| Fase | Chamada | Efeito |
| --- | --- | --- |
| Alocação | POST /files/prepare | Retorna write_id, versão, N destinos e reservas |
| Escrita física | PUT /objects/{write_id} | Persistência, SHA-256 e ACK no controlador |
| Confirmação | POST /files/uploads/{write_id}/ack | Associa nó, disco, tamanho e hash validado |
| Publicação | POST /files/uploads/{write_id}/commit | Torna a versão COMMITTED após W=N ACKs |
| Consulta | GET /files/metadata?path=... | Tamanho, data, hash, status, version_id, réplicas |
| Leitura física | GET /objects/{version_id} | Bytes íntegros, diretamente para CLI |
| Registro | POST /nodes/register e POST /nodes/{id}/heartbeat | Descoberta e capacidades |
| Reconciliação | POST /nodes/{id}/reconcile | Recuperação de cópias após retorno |
| Diagnóstico | GET /status e GET /metrics | Estados, reparos, latência e contagens |

Namespace: `ls`, `mkdir`, `rmdir`, `mv`, `rm`, `put`, `get`, `stat`, `status`. Diretórios e caminhos canônicos são persistidos no PostgreSQL. `mv` renomeia somente arquivos, atualizando metadados, sem cópia de conteúdo.

## 3. Posicionamento, concorrência e consistência

Cada versão ocupa N nós/discos distintos, respeitando `machine_id`, reservas e percentual máximo de uso de disco. A seleção ordena candidatos elegíveis por espaço livre; quando não há N destinos, a gravação é rejeitada.

Semântica de consistência: o upload gera uma versão PENDING, e a versão atual só muda após N ACKs de réplicas verificadas, caracterizando **W = N**. Leituras referem-se sempre à versão publicada e validam SHA-256; não devolvem escritas parciais. O PostgreSQL usa transações, unicidade e bloqueios de linhas. Não existe transação atômica distribuída entre SQL e Storage Nodes: resíduos após interrupções exigem limpeza posterior, uma limitação conhecida.

Em partições que impedem obter W=N, novas escritas podem ser recusadas, priorizando consistência do conteúdo publicado sobre disponibilidade de gravação. Leituras podem continuar enquanto metadados e alguma réplica íntegra permanecerem acessíveis (justificativa CAP).

## 4. Integridade, scrubbing e cifra

O hash SHA-256 é calculado sobre **o texto claro original**, verificado ao gravar, ao ler e periodicamente por scrubbing. Uma réplica com hash divergente é marcada como CORRUPTED e reportada; o controlador deixa de contá-la como saudável, recompõe N e a descarta apenas após validar a substituta.

A configuração `ENCRYPTION_ENABLED` permite AES-256-GCM com nonce aleatório de 96 bits e tag GCM de 128 bits, autenticando o ID físico como AAD. A chave de 256 bits é Base64 em `ENCRYPTION_KEY_BASE64`, exclusivamente no arquivo `.env` privado dos Storage Nodes. Com criptografia ligada, plaintext é acumulado temporariamente em memória e somente ciphertext é persistido em disco. A leitura cifra/decifra conforme o metadado do objeto e valida SHA-256 do plaintext; objetos antigos sem cifra continuam legíveis. Não há KMS ou rotação de chaves.

## 5. Detecção de falhas, re-replicação, drain e retorno

O controlador recebe heartbeats periódicos e classifica nós HEALTHY, SUSPECT ou DEAD por timeout. Quando há déficit de réplicas, seleciona novo nó elegível, valida origem e destino pelo SHA-256 e confirma a cópia. A prioridade favorece arquivos com **menos réplicas vivas**; `MAX_CONCURRENT_RECOVERIES` limita o número de reparos simultâneos e `REPAIR_BANDWIDTH_BYTES_PER_SEC` limita a velocidade **por transferência**.

`drain` altera o nó para DRAINING, não cancela o estado nos heartbeats, impede novos uploads e migra réplicas antes da retirada planejada. Após retorno de um nó, o inventário é confrontado com metadados do controlador; cópias LOST são verificadas fisicamente para reaproveitamento, e versões conhecidas obsoletas podem ser descartadas. A descoberta dinâmica aceita nós com nome interno `storage-N:9000` na rede Docker sem editar a lista central.

## 6. Persistência, visibilidade e logs

PostgreSQL armazena árvore de diretórios, mapa de arquivos, versões, réplicas, nós e discos. Volumes nomeados preservam banco e objetos após a recriação dos contêineres. `/status` mostra nós/discos, arquivos por estado e reparos ativos; `/metrics` agrega contagens de réplicas, requisições, latência média e bytes do gateway. Logs estruturados em JSON têm timestamp UTC, componente, evento, nível e identificadores.

## 7. Evidências práticas obtidas na AWS (17/11/2026)

**Ambiente:** instância EC2 Ubuntu, Docker Compose com cinco storages e PostgreSQL; branch `feature/parte1-rf9-direct-client`, commit `14a6976` nos ensaios. **Suíte integrada: 64 testes aprovados, um aviso de depreciação, em 7,36 segundos.** A versão anterior havia aprovado 52 testes e comprovado RF1/RF4.

- **RF1/RF3 e separação de planos:** upload de 46 bytes via CLI Docker com N=3, três nós distintos, download direto e `cmp` confirmando identidade byte a byte. SHA-256: `2984f43aa700834411dbf9d0ef6d2e0efdce513ec035f8f9111726a98bfb9af9`.
- **RF5 — falha:** após `docker compose stop storage-2`, logs registraram `HEALTHY → SUSPECT → DEAD`, seguidos de eventos `repair_completed`; os seis arquivos continuaram saudáveis após reparação, sem indisponibilidade. Download conservou o SHA-256.
- **RF6 — retorno:** `docker compose start storage-2` resultou em `node_reconciled` com `restored=0, discarded=5, skipped=0`: cinco cópias antigas descartadas porque o cluster já havia restabelecido N em nós elegíveis.
- **RF7 — drain:** storage-1 entrou em `DRAINING`; arquivos passaram temporariamente por `UNDER_REPLICATED`/`RECOVERING`, e a substituição ocorreu em storage-4. Ao término, seis arquivos `HEALTHY`, nenhum indisponível e storage-1 sem dados; `undrain` devolveu o nó a `HEALTHY`.
- **RF8 — concorrência:** script `scripts/test_concurrency_live.py` retornou `PASS`; duas escritas concorrentes no mesmo caminho resultaram em HTTP 409 e HTTP 201; **12/12 leituras** deram HTTP 200 com hashes válidos e a versão final permaneceu íntegra. Ao final, oito arquivos `HEALTHY`.
- **RF9 — AES-256-GCM:** dois testes específicos aprovados. Com `ENCRYPTION_ENABLED=true`, o arquivo de 42 bytes foi persistido em três nós. Inspeção de disco mostrou metadado `AES-256-GCM`, prefixo `DCD1` e ausência do texto claro. Download e `cmp` iguais; um arquivo antigo não cifrado continuou legível. A chave privada está em `.env` fora do Git.
- **RF10:** persistência de cinco arquivos após reiniciar a instância EC2, sem perda de metadados ou das três réplicas. `pg_dump` e cinco tarballs de storage foram conferidos.
- **RF11/RF12:** `/status` mostrou nós, discos, capacidades, estados e reparos; `/metrics` mostrou latência, uptime e contagens. Logs JSON apresentaram `timestamp`, `component`, `event`, `level` e identificadores, incluindo reconciliação e recuperação.

O teste de corrupção anterior do **RF4** detectou uma cópia danificada por scrubbing, recompôs o fator N=3 e preservou o SHA-256 `c538961c3614bf69bafac27ac120868bbbde2d3014b01aa8fbc8c26cd9926f2e`. É necessário incluir a sequência de corrupção/reparação no vídeo ou demonstração presencial.

**Registro detalhado:** `docs/evidencias/parte1/aws-validacao-integrada-2026-11-17.md`. Não confundir testes funcionais com medição de disponibilidade em falhas físicas ou com auditoria de segurança.

## 8. Limitações conhecidas

- Cinco storages no mesmo host físico, sem tolerância à perda de toda a EC2.
- Controlador e PostgreSQL únicos: reinicialização possível, mas não alta disponibilidade.
- `rm` não mantém fila durável de limpeza se um nó estiver indisponível.
- Cliente direto e reparador carregam objetos completos em memória; não existe chunking independente.
- Gateway do dashboard ainda encaminha bytes pelo controlador; apenas CLI direta separa plano de dados.
- Limite de banda de recuperação é por tarefa, não garantia de limite agregado.
- Sem autenticação de usuários, autorização por arquivo, TLS interno, KMS ou rotação criptográfica.
- A operação `mv` não move diretórios inteiros; somente arquivos.
- A execução ao vivo comprovou concorrência e AES-GCM, mas **N diferente de 3, rejeição por falta de espaço e entrada de nó adicional** ainda não tinham evidência prática nos primeiros ensaios. Existe script específico de validação RF3/RF7 em `scripts/test_rf3_allocation_live.py` para execução posterior.
- O relatório documenta N de contêineres distintos no mesmo host, não sobrevivência a falha de máquina física.
- Os contadores de tráfego do controlador não incluem o caminho direto CLI–Storage.

## 9. Divisão de tarefas e comunicação

- **Joseph Antony:** arquitetura, controlador e metadados, PostgreSQL, AWS, interface web, integração.
- **Thayla Figueiredo:** Storage Nodes, persistência física, hash, scrubbing e gerenciamento de capacidade.
- **Jeferson Gonzaga:** CLI, protocolos de comunicação, recuperação de falhas e testes.

Todos devem manter contribuições atribuídas corretamente no Git e revisar o relatório antes da entrega. Os PRs permanecem empilhados até serem integrados com histórico preservado.

## 10. Matriz RF1–RF12 e entregáveis

| Código | Evidência comprovada / ressalvas |
| --- | --- |
| RF1 | Operações de namespace, estatísticas, SHA-256, data, cópias e estados; upload/download comprovados na AWS |
| RF2 | CLI direta, endpoints REST e interface web opcional |
| RF3 | N=3 comprovado, nós/discos lógicos distintos; teste de N=2 preparado, ainda pendente de execução |
| RF4 | SHA-256, scrubbing e corrupção/reparação comprovados em ensaio anterior |
| RF5 | Falha real, substituição e retomada N=3; prioridade e limites têm testes automatizados |
| RF6 | Registro, retorno e reconciliação comprovados, com cinco cópias obsoletas descartadas |
| RF7 | Drain completo comprovado; rejeição por capacidade tem script de teste pendente |
| RF8 | Concorrência real: duas escritas (409/201) e 12 leituras íntegras, PASS |
| RF9 | AES-256-GCM em disco, download idêntico, leitura retrocompatível, testes aprovados |
| RF10 | Persistência após reinicialização da EC2 e backups conferidos |
| RF11 | /status e /metrics demonstrados; métricas de tráfego direto não agregadas |
| RF12 | Logs JSON de heartbeat, reparo, scrubbing e reconciliação demonstrados |

Entregáveis: repositório com contribuições da equipe, README, **relatório PDF com 4 a 8 páginas**, Compose com 1 controlador e ao menos 5 Storage Nodes e **vídeo até 5 minutos ou apresentação presencial** com upload N, leitura, queda/reparo e corrupção detectada.

**Fonte:** enunciado oficial da disciplina, *Atividade Prática: Sistema Distribuído de Armazenamento de Arquivos* (UFS, 2026), páginas 1–5. A Parte 2, com medições e análise experimental, vence em 30/11/2026.
