"""Servidor MCP da Central de Salas (Streamable HTTP, stateless, 2026-07-28).

Tres tools e o resource politica://uso. Quando a sala pedida esta ocupada, o
resolver escolha_de_sala devolve um Elicit: o SDK responde input_required e
sela o requestState com a chave de REQUEST_STATE_SECRET. Nada da rodada fica
em memoria do processo entre o pedido e o retry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, ElicitationResult, Resolve
from mcp.server.request_state import RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, create_model

DADOS = Path(__file__).resolve().parent.parent / "dados"
SAO_PAULO = timezone(timedelta(hours=-3))
ABRE, FECHA = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_ALTERNATIVAS = 3
VALIDADE_DO_ESTADO = 10 * 60


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


def ler_segredo() -> bytes:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  export REQUEST_STATE_SECRET="$(python3 -c \'import secrets; print(secrets.token_hex(32))\')"'
        )
    return segredo.encode()


SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {sala["id"]: sala for sala in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def instante(valor: str) -> datetime:
    try:
        momento = datetime.fromisoformat(valor)
    except ValueError as erro:
        raise ToolError(f"Data invalida: {valor}") from erro
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=SAO_PAULO)
    return momento


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Regras de sala e de politica, iguais na consulta e na reserva."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = instante(inicio), instante(fim)
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_local, fim_local = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    if ini_local.time() < ABRE or fim_local.date() != ini_local.date() or fim_local.time() > FECHA:
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        reserva
        for reserva in RESERVAS
        if reserva["sala"] == sala and instante(reserva["inicio"]) < fi and ini < instante(reserva["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        candidata
        for candidata in SALAS
        if candidata["id"] != sala
        and candidata["capacidade"] >= capacidade
        and not conflitos(candidata["id"], ini, fi)
    ]
    livres.sort(key=lambda candidata: (candidata["capacidade"], candidata["id"]))
    return [candidata["id"] for candidata in livres[:MAXIMO_ALTERNATIVAS]]


def proximo_id() -> str:
    maior = max((int(reserva["id"].split("-")[1]) for reserva in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[ler_segredo()], ttl=VALIDADE_DO_ESTADO),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**sala) for sala in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    achados = conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not achados,
        conflitos=[ConflitoOut(**{chave: reserva[chave] for chave in ("id", "inicio", "fim", "responsavel")}) for reserva in achados],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """So pergunta quando a sala pedida esta ocupada e existe alternativa.

    Roda de novo em cada rodada. O SDK so consome a resposta do cliente se a
    pergunta renderizada agora for identica a que foi selada no requestState.
    """
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    escolha = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", escolha)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    # Argumentos que divergem do que foi selado nem chegam aqui: o SDK recusa
    # o requestState com -32602 antes de entrar na tool.
    if escolha.action in ("decline", "cancel"):
        motivo = "recusado" if escolha.action == "decline" else "cancelado"
        return ReservaOut(reservado=False, motivo=motivo)
    destino = escolha.data.sala if escolha.data is not None else sala
    ini, fi = validar(destino, inicio, fim)
    if conflitos(destino, ini, fi):
        raise ToolError("Sem alternativas disponiveis no intervalo")
    nova = {"id": proximo_id(), "sala": destino, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(nova)
    log(f"reserva criada {nova['id']} sala={destino} {inicio}..{fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=nova["id"],
        reservado=True,
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    mime_type="text/markdown",
    description="Politica de uso das salas. A primeira linha declara a versao.",
)
def politica() -> str:
    return POLITICA_TEXTO


class LogDeRequests:
    """Registra no stderr o metodo, o id e o traceparent de cada request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mais = True
        while mais:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            mais = mensagem.get("more_body", False)
        self.registrar(corpo)
        entregue = False

        async def reenviar() -> dict:
            nonlocal entregue
            if entregue:
                return await receive()
            entregue = True
            return {"type": "http.request", "body": corpo, "more_body": False}

        await self.app(scope, reenviar, send)

    @staticmethod
    def registrar(corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            log("mcp request ilegivel")
            return
        for mensagem in dados if isinstance(dados, list) else [dados]:
            if not isinstance(mensagem, dict):
                continue
            params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            log(
                f"mcp request method={mensagem.get('method')} id={mensagem.get('id')!r}"
                f" traceparent={meta.get('traceparent', '-')}"
            )


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    log(f"servidor MCP central-de-salas em http://{host}:{porta}/mcp")
    uvicorn.run(LogDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
