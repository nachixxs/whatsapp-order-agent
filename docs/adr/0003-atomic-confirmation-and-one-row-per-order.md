# 0003. Atomic confirmation: one row per order, and never "Listo" without a row

Status: accepted (2026-09-27). Rules: R1 to R6, R10, R25, R28.

## Context

The two worst failures for an order bot are telling the customer "¡Listo!" for an order that was not written,
and writing the same order twice. The second one is easy to cause: customers send "sí" and "dale" a second
apart, and the webhook is processed in the background, so two messages of one conversation can be in flight
together (spec, R4).

## Decision

- A complete order does not touch the sheet. It waits in SQLite as pending while the customer reads a summary.
- `confirmar_pedido(acepta=true)` triggers `memoria.tomar_para_confirmar`, one transaction that removes the
  pending order, raises the generation number and records the take. A second "sí" finds nothing to take.
- The sheet row is appended in exactly one place, `confirmacion.confirmar`, with `value_input_option="RAW"`
  and no retry. `gspread` is created with the plain `HTTPClient`, because the back-off client retries on
  `429` and `5xx`, and a retry after a timed-out write that succeeded leaves a second row. The timeout is
  5 seconds to connect and 180 to read (`app/sheets.py`).
- If the write fails, the order goes back to pending, the customer gets the save-error message and a new "sí"
  retries. `¡Listo!` exists in one string, `MENSAJE_PEDIDO_CONFIRMADO`, returned only after the row is written.
- Once the row exists, nothing that fails afterwards (closing the take, the internal note) changes the reply.
- A message that arrives in the five minutes after the write, and changes nothing, gets no reply.

## Alternatives

- **Reply first, write afterwards.** Rejected: this is the failure being avoided.
- **Retry the write.** Rejected, see above.
- **A database with row locks.** Not needed: one process with one SQLite file and one lock per conversation
  covers the concurrency this bot has, and the server enforces one worker.

## Consequences

- A failed write costs the customer one more "sí".
- A take held by a process that died is released after five minutes without returning the order to pending
  (R25). If that process died before writing, the order is lost and the customer never got "¡Listo!". The spec
  prefers that to risking a second row.
- The bot cannot scale horizontally without replacing the in-memory lock and the SQLite file.
