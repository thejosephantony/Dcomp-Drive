# Evidência de execução real do RF1 — 17/11/2026

**Ambiente:** instância AWS EC2 Ubuntu; Docker Compose com controlador FastAPI, PostgreSQL e cinco Storage Nodes. Branch validada: `feature/rf1-http-namespace` (`a7940dc`). API do controlador exposta somente em `127.0.0.1:8000`.

## Cenário testado

1. Suíte automatizada em contêiner: **52 passed, 1 warning**, sem falhas.
2. `mkdir` criou duas pastas no catálogo persistente PostgreSQL.
3. `ls` listou pastas e arquivos já existentes.
4. `put` gravou 47 bytes com **N=3** nas instâncias lógicas `storage-1`, `storage-2` e `storage-3`.
5. `mv` renomeou o caminho lógico, sem alterar tamanho, versão, hash ou localização das réplicas.
6. `stat` confirmou metadados e as três réplicas após a movimentação.
7. `get` recuperou conteúdo byte a byte idêntico ao original.
8. `rm` removeu a entrada de arquivo de teste; `rmdir` removeu os dois diretórios vazios.
9. `ls /` final confirmou a permanência dos cinco arquivos anteriores, todos com estado `HEALTHY`.

## Verificação de integridade

SHA-256 original e recuperado:

`15cc5c6fc010734c3757cc449f147353785f54d0b691cbee0642610cfb6d0c1f`

## Documentação dos logs

Consulte [rf1-operacoes-aws.log](rf1-operacoes-aws.log), transcrição dos trechos relevantes do registro de terminal fornecido pelo operador.

## Limitações

O ensaio confirma operações usuais do RF1, não garante todos os interleavings concorrentes. O `rm` ainda não dispõe de fila durável para exclusão física pendente quando Storage Nodes ficam indisponíveis. Os cinco nós de armazenamento são contêineres no mesmo host EC2, não cinco máquinas físicas distintas. Esta documentação foi organizada a partir da saída do terminal enviada pelo operador; não foi coletada diretamente por este agente na EC2.
