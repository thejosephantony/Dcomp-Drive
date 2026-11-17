# DComp Drive — Relatório Técnico da Parte 1

**Universidade Federal de Sergipe • Sistemas Distribuídos • Prof. Rafael Oliveira Vasconcelos**  
**Equipe:** Joseph Antony, Thayla Figueiredo e Jeferson Gonzaga  
**Data:** 17/11/2026 | **Entrega da Parte 1:** 18/11/2026  
**Versão:** documento para revisão e confirmação das novas funcionalidades na AWS. O PDF de seis páginas é disponibilizado separadamente como artefato de entrega.

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

## 7. Evidências práticas obtidas na AWS

A versão anterior às novas mudanças de conformidade passou em **52 testes, 1 aviso**, em 5,78 s. Um ensaio RF1 real executou `mkdir → ls → put → stat → mv → get → rm → rmdir` com arquivo de 47 bytes, N=3 e hashes idênticos antes e depois da movimentação. Outros cinco arquivos permaneceram íntegros após a remoção dos recursos de teste.

No teste de corrupção RF4, o arquivo `/rf4-corrupcao.txt` tinha inicialmente três réplicas em storage-1, storage-2 e storage-4. A corrupção de uma cópia no storage-1 foi detectada por scrubbing e confirmada pelo controlador. O reparador recompôs a cópia no storage-5 e descartou a corrompida, restabelecendo HEALTHY/3 de 3. O arquivo recuperado apresentou SHA-256 igual ao original, `c538961c3614bf69bafac27ac120868bbbde2d3014b01aa8fbc8c26cd9926f2e`. Evidências estão em `docs/evidencias/parte1/`.

**Transparência:** módulos mais recentes (RF5, RF6, RF7, RF9, RF11, RF12 e CLI direta) foram preparados em PRs adicionais, mas **aguardam execução de testes completos e validação integrada na EC2**. Não atribuir o selo "validado" antes dos resultados.

## 8. Limitações conhecidas

- Cinco storages no mesmo host físico, sem tolerância à perda de toda a EC2.
- Controlador e PostgreSQL únicos: reinicialização possível, mas não alta disponibilidade.
- `rm` não mantém fila durável de limpeza se um nó estiver indisponível.
- Cliente direto e reparador carregam objetos completos em memória; não existe chunking independente.
- Gateway do dashboard ainda encaminha bytes pelo controlador; apenas CLI direta separa plano de dados.
- Limite de banda de recuperação é por tarefa, não garantia de limite agregado.
- Sem autenticação de usuários, autorização por arquivo, TLS interno, KMS ou rotação criptográfica.
- A operação `mv` não move diretórios inteiros; somente arquivos.
- Teste de concorrência real, entrada de novos nós, alteração de N e ensaio físico de AES-GCM ainda precisam de resultados documentados.

## 9. Divisão de tarefas e comunicação

- **Joseph Antony:** arquitetura, controlador e metadados, PostgreSQL, AWS, interface web, integração.
- **Thayla Figueiredo:** Storage Nodes, persistência física, hash, scrubbing e gerenciamento de capacidade.
- **Jeferson Gonzaga:** CLI, protocolos de comunicação, recuperação de falhas e testes.

Todos devem manter contribuições atribuídas corretamente no Git e revisar o relatório antes da entrega. Os PRs permanecem empilhados até serem integrados com histórico preservado.

## 10. Matriz RF1–RF12 e entregáveis

| Código | Estado do trabalho | Evidência pendente |
| --- | --- | --- |
| RF1 | Operações funcionais + data/RECOVERING adicionados | Regressão de metadados novos |
| RF2 | CLI, REST e web opcional | Validado anteriormente |
| RF3 | N configurável, nós/discos distintos | N=3 validado |
| RF4 | SHA-256 e scrubbing | Corrupção/reparo demonstrados |
| RF5 | Falha, reparo prioritário e limites | Testar prioridade e limites novos |
| RF6 | Descoberta e reconciliação | Retorno com cópia íntegra/obsoleta |
| RF7 | Capacidade e drain | Teste real de drenagem planejada |
| RF8 | Versionamento e transações | Concorrrência real com script reproduzível |
| RF9 | AES-256-GCM opcional | Validar serviço ativo e ciphertext no disco |
| RF10 | Metadados persistentes | Reinicialização demonstrada |
| RF11 | Diagnóstico detalhado | Consultar /status e /metrics na AWS |
| RF12 | Logs JSON | Capturar eventos com timestamp e objeto |

Entregáveis: repositório com contribuições da equipe, README com os comandos, **relatório de 4 a 8 páginas**, Docker Compose com 1 controlador e 5 Storage Nodes, e **demonstração em vídeo ≤ 5 minutos ou apresentação presencial** mostrando upload/replicação, leitura, falha/recuperação e corrupção detectada.

**Fonte:** Enunciado oficial da disciplina, *Atividade Prática: Sistema Distribuído de Armazenamento de Arquivos* (UFS, 2026), páginas 1–5. A Parte 2, com medições e análise experimental, vence em 30/11/2026.
