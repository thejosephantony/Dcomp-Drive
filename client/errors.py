"""Erros do cliente, mapeados para os códigos do contrato de API do plano
(seção 05: 404 caminho inexistente, 409 conflito, 507 sem capacidade/destinos)."""


class CLIError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(CLIError):
    def __init__(self, message: str) -> None:
        super().__init__("404", message)


class ConflictError(CLIError):
    def __init__(self, message: str) -> None:
        super().__init__("409", message)


class InsufficientCapacityError(CLIError):
    def __init__(self, message: str) -> None:
        super().__init__("507", message)


class UnavailableError(CLIError):
    """Nenhuma réplica íntegra disponível para leitura (estado UNAVAILABLE)."""

    def __init__(self, message: str) -> None:
        super().__init__("503", message)
