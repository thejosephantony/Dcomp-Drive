# Primeira integração HTTP do DComp Drive — gateway N=3

Esta branch depende de feat/storage-nodes (PR #9). A CLI passa a ter o
backend HTTP, enquanto o controlador transfere os bytes aos Storage Nodes
pela rede interna. Não são necessários endereços públicos para cada nó.

## Escopo

- PUT /files/content?path=/arquivo.bin no controlador:
  - lê bytes com limite (32 MiB por padrão);
  - calcula SHA-256 e reserva N máquinas diferentes via PREPARE;
  - envia PUT /objects/{write_id} a cada Storage Node;
  - exige ACK e relê os bytes gravados para verificar integridade;
  - publica a versão com COMMIT apenas após as N cópias verificadas;
  - em erro, aborta a versão e tenta limpar as cópias parciais.
- GET /files/content?path=/arquivo.bin:
  - resolve a versão COMMITTED e tenta réplicas saudáveis em sequência;
  - valida tamanho e SHA-256 de cada resposta;
  - não retorna dados corrompidos.
- DCOMP_BACKEND=http seleciona o novo client/http_backend.py.
- put, get, stat e status passam pelo controlador.
- Demais comandos continuam apenas no MockBackend; no modo HTTP retornam 501.

Cada versão usa o write_id como object_id físico. Versões antigas
continuam nos nós até existir política de coleta de lixo. Não modificar
essa convenção sem planejar a migração dos dados já gravados.

## Testes sem Docker (Windows PowerShell)

A suíte usa bancos SQLite descartáveis e Storage Nodes simulados em memória.
Isso testa o protocolo, não substitui a integração com PostgreSQL na AWS.

~~~powershell
cd "$HOME\Downloads\Dcomp-Drive"
git fetch origin
git worktree add --detach ..\Dcomp-Drive-integracao origin/feature/http-gateway-cli-n3
cd ..\Dcomp-Drive-integracao
py -m pip install -r requirements.txt
py -m pytest -q tests
~~~

A suíte inclui os 29 testes anteriores e os novos testes de gateway/CLI.

### Teste ponta a ponta sem Docker

Para verificar cinco processos reais, três arquivos físicos e recuperação de
leitura após a queda de um nó (SQLite descartável em vez de PostgreSQL):

~~~powershell
py .\scripts\smoke_gateway_local.py
~~~

O script inicia e encerra automaticamente todos os serviços na interface
127.0.0.1; os dados de teste ficam em uma pasta temporária.

## Teste integrado na AWS ou em servidor Linux

Após configurar uma rede privada para os contêineres, credenciais de
PostgreSQL e volumes persistentes, inicie o stack com Docker Compose.

~~~bash
cp .env.example .env
# Configure POSTGRES_PASSWORD forte; não versione .env
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/status
~~~

No computador cliente, com acesso autorizado ao endpoint do controlador:

~~~powershell
$env:DCOMP_BACKEND="http"
$env:DCOMP_CONTROLLER_URL="http://127.0.0.1:8000"
"Arquivo de teste" | Set-Content teste.txt
py -m client.cli put teste.txt /teste.txt
py -m client.cli stat /teste.txt
py -m client.cli get /teste.txt recuperado.txt
py -m client.cli status
Remove-Item Env:\DCOMP_BACKEND
Remove-Item Env:\DCOMP_CONTROLLER_URL
~~~

Substitua o endereço do controlador pelo endpoint acessível através de
um túnel SSH ou VPN. Não exponha o controlador e os nós sem autenticação.

## Limitações e próximos passos

- Limite de 32 MiB por operação (MAX_TRANSFER_BYTES) e buffers em memória;
  não indicado para arquivos grandes ou muitos usuários simultâneos.
- O ACK ainda pode ser falsificado pela API antiga. O gateway relê os
  dados para verificar integridade, mas falta autenticação mútua entre nós.
- A API de registro e os endpoints de arquivo não autenticam usuários.
  **Não implantar publicamente** sem controles de rede e autenticação.
- O controlador pode falhar entre a persistência dos bytes e os metadados,
  deixando objetos órfãos. Falta reconciliação e limpeza periódica.
- O gateway usa a API síncrona SQLAlchemy dentro de rotas assíncronas;
  adequar para carga de produção depois da demonstração.
- Sobrescritas preservam versões antigas, mas ainda não têm garbage collection.
- Não há re-replicação automática após perda física de um nó.
- O GitHub Actions continua falhando em pré-execução; validar
  separadamente a configuração de runners/Actions.
- ls, mkdir, rm, mv, rmdir e drain ainda não têm implementação HTTP.

A integração precisa ser validada com três réplicas reais em PostgreSQL
antes de aprovação e merge. A branch é um PR em rascunho.
