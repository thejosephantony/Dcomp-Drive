"""CLI do DComp Drive (python -m client.cli <comando>).

Por enquanto fala só com o MockBackend (ver client/mock_backend.py). Trocar
para o backend real vai significar mudar apenas a função _backend() abaixo.
"""

from pathlib import Path
from typing import Any, Callable

import typer

from client.backend import Backend
from client.errors import CLIError
from client.mock_backend import MockBackend

app = typer.Typer(help="DComp Drive — cliente de linha de comando (modo mock)")


def _backend() -> Backend:
    return MockBackend()


def _call(fn: Callable[..., Any], *args: object, **kwargs: object) -> Any:
    """Executa a chamada ao backend e traduz CLIError em saída amigável.

    Sem genérico de tipo (PEP 695) de propósito: evita depender do Python
    3.12+ caso a imagem Docker da equipe fixe uma versão mais antiga.
    """
    try:
        return fn(*args, **kwargs)
    except CLIError as exc:
        typer.secho(f"ERRO [{exc.code}] {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def put(local_path: Path, remote_path: str) -> None:
    """Envia um arquivo local para um caminho no DComp Drive."""
    meta = _call(_backend().put, local_path, remote_path)
    typer.echo(f"OK  {meta.path}  v{meta.version}  sha256={meta.sha256}  estado={meta.state.value}")


@app.command()
def get(remote_path: str, local_path: Path) -> None:
    """Baixa um arquivo do DComp Drive para um caminho local."""
    meta = _call(_backend().get, remote_path, local_path)
    typer.echo(f"OK  {local_path}  v{meta.version}  sha256={meta.sha256}")


@app.command()
def ls(remote_path: str = typer.Argument("/")) -> None:
    """Lista o conteúdo de um diretório.

    Valor padrão vira argumento posicional, não opção, por isso o
    typer.Argument(...) explícito (ver client/cli.py: só esse comando tem
    default entre os parâmetros de caminho).
    """
    entries = _call(_backend().ls, remote_path)
    if not entries:
        typer.echo("(vazio)")
        return
    for entry in entries:
        marker = "/" if entry.kind == "dir" else ""
        detail = f"  {entry.size}B  {entry.state}" if entry.kind == "file" else ""
        typer.echo(f"{entry.name}{marker}{detail}")


@app.command()
def mkdir(remote_path: str) -> None:
    """Cria um diretório."""
    _call(_backend().mkdir, remote_path)
    typer.echo(f"OK  diretório criado: {remote_path}")


@app.command()
def rmdir(remote_path: str) -> None:
    """Remove um diretório vazio."""
    _call(_backend().rmdir, remote_path)
    typer.echo(f"OK  diretório removido: {remote_path}")


@app.command()
def mv(src: str, dst: str) -> None:
    """Move/renomeia um arquivo."""
    _call(_backend().mv, src, dst)
    typer.echo(f"OK  {src} -> {dst}")


@app.command()
def rm(remote_path: str) -> None:
    """Remove um arquivo."""
    _call(_backend().rm, remote_path)
    typer.echo(f"OK  removido: {remote_path}")


@app.command()
def stat(remote_path: str) -> None:
    """Mostra metadados e réplicas de um arquivo."""
    meta = _call(_backend().stat, remote_path)
    typer.echo(f"caminho   {meta.path}")
    typer.echo(f"versão    {meta.version}")
    typer.echo(f"tamanho   {meta.size} bytes")
    typer.echo(f"sha256    {meta.sha256}")
    typer.echo(f"estado    {meta.state.value}")
    for replica in meta.replicas:
        typer.echo(f"réplica   {replica.node_id}  {replica.state.value}")


@app.command()
def status() -> None:
    """Mostra o estado do cluster simulado: nós, uso e arquivos por estado."""
    cluster = _call(_backend().status)
    typer.echo("nós:")
    for node in cluster.nodes:
        typer.echo(f"  {node.node_id}  {node.state.value}  {node.used_bytes}/{node.capacity_bytes} bytes")
    typer.echo("arquivos por estado:")
    for state, count in cluster.files_by_state.items():
        typer.echo(f"  {state}: {count}")


@app.command()
def drain(node_id: str) -> None:
    """Marca um nó para retirada planejada (sem redistribuição no modo mock)."""
    _call(_backend().drain, node_id)
    typer.echo(f"OK  nó em drain: {node_id} (redistribuição ainda não implementada no mock)")


if __name__ == "__main__":
    app()
