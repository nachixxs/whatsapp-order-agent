# 0002. The agent keeps its own history, with markers instead of prose

Status: accepted (2026-09-27). Rules: R20, R21, R22.

## Context

Chatwoot stores the whole conversation, including the exact sentences the bot sent. Sending those sentences
back to the model as its own previous turns looks natural. On the order bot this one replaces, it did not
work: the spec records that with prose in the history the model called a tool 0 times out of 3, and with
markers 3 out of 3 (spec, R21). That measurement was made on the previous bot and has not been repeated in
this repository.

## Decision

- The bot stores the customer's messages in SQLite (`charlas` table in `app/memoria.py`) and, for its own
  turns, a marker that says what it did: `[dato_faltante: material]`, `[consulta_general: horarios]`,
  `[pedido_pendiente_confirmacion]`. The prose of the reply is not stored.
- The system prompt explains the markers and tells the model not to write them (`app/prompt.py`).
- The history is capped at 20 messages and expires after 6 hours, so a "sí" tomorrow cannot confirm
  today's summary. It always starts with a `user` message, and thinking blocks are never stored.
- The internal detail of a tool result never enters the history.

## Alternatives

- **Read the conversation from Chatwoot on every turn.** Rejected: one more HTTP call on the path of every
  message, and the history would be prose again.
- **Store the prose.** Rejected, for the measurement above.
- **No history.** Rejected: "sí" and "el viernes" mean nothing without the previous turn.

## Consequences

- There are two sources of state, SQLite and Chatwoot. SQLite is a file with one process holding it, which
  is why the server refuses to start with more than one worker (R28).
- The model cannot see the exact words it used with the customer. It knows what it did, not how it phrased it.
- `memoria.db` holds customer message text and the order in progress for up to 6 hours; a sweep that runs
  on each incoming batch deletes what expired. The file opens with `secure_delete` on and the write-ahead log
  is truncated, so deleted text does not stay readable in it (R27).
