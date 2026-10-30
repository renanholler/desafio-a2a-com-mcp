# A Ponte

Servidor MCP da Central de Salas e agente A2A que o consome por HTTP. O agente não decide conflito, política nem alternativa: traduz protocolo.

## Como rodar

Python 3.10 ou superior. Os dois processos são separados. Suba o servidor MCP antes do agente.

Gere a chave do `requestState` e exporte no terminal do servidor. Não commite o valor.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
export REQUEST_STATE_SECRET="<o valor impresso>"
```

Servidor MCP, na porta 7301:

```bash
cd servidor-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
REQUEST_STATE_SECRET="$REQUEST_STATE_SECRET" .venv/bin/python servidor.py
```

Agente, em outro terminal, na porta 7300:

```bash
cd agente
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python agente.py
```

Validador, com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

`MCP_PORT` e `AGENTE_PORT` mudam as portas. O padrão é 7301 e 7300. `MCP_URL` aponta o agente para o servidor. `AGENTE_URL` entra na URL da interface do Agent Card.

## Onde a ponte acontece

Em `aplicar_resultado`, em [agente/agente.py](agente/agente.py), um `InputRequiredResult` do MCP vira `TASK_STATE_INPUT_REQUIRED`. O `requestState`, a chave do `inputRequests`, o enum e os argumentos originais ficam numa `Pausa` ligada à Task, fora do JSON devolvido ao cliente A2A. A mensagem da Task é a linha `alternativas: ...`, na ordem do enum.

Em `continuar`, no mesmo arquivo, `escolha=<id>` repete `tools/call` com id JSON-RPC novo. `HostMCP.chamar` manda `inputResponses` na mesma chave e o `requestState` byte a byte. `escolha=recusar` vai como `action: decline`, e a Task termina em `TASK_STATE_CANCELED`.

## Decisões técnicas

O `requestState` é selado pelo `RequestStateSecurity` do SDK MCP 2.3.0, com AES-256-GCM. A chave vem de `REQUEST_STATE_SECRET`, com no mínimo 32 bytes, e não está no código. O TTL é 10 minutos. O nome do servidor, `central-de-salas`, é a audience do selo, então um retry continua válido depois de reiniciar o processo, desde que a mesma variável de ambiente esteja exportada. As reservas vivem numa lista em memória no servidor e somem no restart. As Tasks e as pausas vivem em memória no agente.

O cliente MCP declara só elicitation em form mode. As chamadas usam `allow_input_required=True`, então o agente vê o `input_required` cru e não responde a pergunta sozinho.

## Saída do validador

```
trace-id desta execucao: 6d214c09cd61ce1385616f8228891ce2
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
