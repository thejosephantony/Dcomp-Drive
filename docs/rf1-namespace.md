# RF1 — Operações de arquivos e diretórios no cluster real

Este módulo amplia a CLI HTTP com `ls`, `mkdir`, `rmdir`, `mv` e `rm`. Os comandos anteriores `put`, `get`, `stat` e `status` permanecem disponíveis.

## Antes de usar

Na EC2, execute a versão da branch, confirme `sudo docker compose ps`, `curl -fsS http://127.0.0.1:8000/health` e toda a suíte `pytest -q tests`. Não use `down -v`. Os comandos abaixo são **somente** para o Ubuntu, dentro de `~/Dcomp-Drive`, e criam um caminho descartável `/rf1-demo`.

**Observação:** do contêiner controller, use `http://127.0.0.1:8000`; do Windows, utilize o túnel SSH já existente.

## Sequência de demonstração

O backend padrão da CLI é `mock`; para demonstrar o cluster **real**, configure o ambiente de cada invocação. A opção mais prática é criar um alias na sessão do Ubuntu:

```bash
dcomp() {
  sudo docker compose exec -T \
    -e DCOMP_BACKEND=http \
    -e DCOMP_CONTROLLER_URL=http://127.0.0.1:8000 \
    controller python -m client.cli "$@"
}

dcomp ls /
dcomp mkdir /rf1-demo
dcomp mkdir /rf1-demo/docs
dcomp ls /rf1-demo

# Envia um arquivo a partir de um path que exista dentro do controller:
# ou use o gateway curl como no teste real:
printf 'Teste RF1: metadados e replicas preservadas.\n' >/tmp/rf1-demo.txt
curl -fsS -X PUT --data-binary @/tmp/rf1-demo.txt \
  'http://127.0.0.1:8000/files/content?path=%2Frf1-demo%2Fdocs%2Farquivo.txt'

dcomp ls /rf1-demo/docs
dcomp stat /rf1-demo/docs/arquivo.txt
dcomp mv /rf1-demo/docs/arquivo.txt /rf1-demo/renomeado.txt
dcomp stat /rf1-demo/renomeado.txt
dcomp rm /rf1-demo/renomeado.txt
dcomp rmdir /rf1-demo/docs
dcomp rmdir /rf1-demo
dcomp ls /
```

Os comandos `mv` e `rm` não alteram outros caminhos do cluster. Remover um diretório não vazio retorna HTTP 409.

## Contratos REST

| CLI | HTTP | Recurso |
| --- | --- | --- |
| ls | GET | `/files/entries?path=/` |
| mkdir | POST | `/files/directories` com `{"path":"/pasta"}` |
| rmdir | DELETE | `/files/directories?path=/pasta` |
| mv | POST | `/files/move` com `{"src":"/antigo","dst":"/novo"}` |
| rm | DELETE | `/files/entry?path=/arquivo` |

As operações são persistidas no PostgreSQL. `mv` altera somente a referência lógica do arquivo, preservando os mesmos IDs físicos de réplica e hash SHA-256.

## Limitações e integridade

- `mv` **não move diretórios**, apenas arquivos. O requisito de mover arquivos é atendido para o cenário descrito.
- `rmdir` só remove diretórios vazios, sem subdiretórios e sem arquivos (inclusive uploads ainda pendentes).
- `rm` remove a entrada lógica no PostgreSQL e tenta apagar **todas as versões físicas** nos Storage Nodes. Caso um nó esteja indisponível, a exclusão remota pode ficar pendente. Isso é reportado como `pending_cleanup` na API e aviso na CLI; ainda não existe fila durável de garbage collection.
- Caminhos remotos precisam ser absolutos e canônicos; não aceitam `..`, `.`, `//`, espaços externos e barras finais.
- A implementação não autentica o cliente ou nós: mantenha o controller restrito ao loopback, com túnel SSH para acesso externo.
- Teste de namespace + tolerância a falhas concorrentes exigirá cenários adicionais antes da Parte 2.

## Testes

```bash
sudo docker compose exec -T controller python -m pytest -q tests
```

Coberturas acrescentadas: operações REST reais da CLI, ciclo `mkdir→put→ls→mv→get→rm→rmdir`, nomes inválidos, diretórios não vazios, versões antigas e limpeza física.
