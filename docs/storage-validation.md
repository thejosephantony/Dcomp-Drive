# Validação do PR #9 — Storage Nodes (sem Docker)

Este PR adiciona testes permanentes ao armazenamento. Os testes isolados
podem rodar diretamente no Windows, sem PostgreSQL nem Docker.

## Executar no PowerShell

Na cópia isolada criada com git worktree:

~~~powershell
cd "$HOME\Downloads\Dcomp-Drive-storage"
git fetch origin
git switch --detach origin/feat/storage-nodes
py -m pip install -r requirements.txt
py -m pytest -q tests
~~~

A suíte preserva os testes do controlador e do protocolo de arquivos,
adicionando testes de persistência e integridade dos Storage Nodes.

**Importante:** a aprovação dos testes não implica integração com o
controlador PostgreSQL nem recuperação automática do fator N=3.

## Reexecutar o smoke test de dois nós

Para o teste externo com dois processos locais, defina as opções abaixo
apenas na sessão PowerShell de teste:

~~~powershell
$env:ALLOWED_STORAGE_PEERS="http://127.0.0.1:19081,http://127.0.0.1:19082"
$env:ENABLE_TEST_ENDPOINTS="true"
py .\testar_branch_storage.py
Remove-Item Env:\ALLOWED_STORAGE_PEERS -ErrorAction SilentlyContinue
Remove-Item Env:\ENABLE_TEST_ENDPOINTS -ErrorAction SilentlyContinue
~~~

Esses endereços são somente os dois processos locais do smoke test.
Em Docker Compose, os endereços internos padrão são storage-1..storage-5
na porta 9000. Alterações de topologia devem ajustar
ALLOWED_STORAGE_PEERS em todos os nós.

O endpoint /objects/{object_id}/corrupt fica desabilitado por padrão e
só deve ser habilitado em ambientes isolados para testes controlados.

## Restrições antes da AWS

- A allowlist reduz o risco de requisições para destinos arbitrários,
  mas **não autentica** os chamadores. Os nós e o controlador precisam
  ficar em rede privada, com acesso restrito por Security Groups.
- As rotas PUT, GET e DELETE ainda não exigem autenticação.
- ACKs do controlador também não são autenticados nem vinculados
  criptograficamente ao objeto armazenado.
- A substituição do arquivo e dos metadados JSON não forma uma transação
  atômica única em caso de interrupção abrupta.
- O sistema não recompõe automaticamente N=3 após perder um nó.

Não integrar nem expor à Internet antes de revisar essas limitações.
