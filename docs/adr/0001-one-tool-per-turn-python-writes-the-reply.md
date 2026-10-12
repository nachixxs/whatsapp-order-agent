# 0001. One tool call per turn, and Python writes every reply

Status: accepted (2026-09-28, revised for `claude-sonnet-5-5`). Rules: R11, R13, R16, R17, R19.

## Context

The agent must turn a free-form WhatsApp message into one action: add order fields, ask for a missing field,
answer an FAQ, hand off to a person, or confirm. The hard product rules are that the bot never quotes a
price, a deadline or a deposit, and never offers an alternative when something is out of stock.

The order bot this one replaces sometimes answered in prose when it was allowed to (recorded in the spec,
R11), and the customer received a generic "I didn't understand". The model chosen here, `claude-sonnet-5-5`,
answers `400` to `tool_choice: {"type": "any"}` (spec, R11), so the usual way to force a tool is not
available.

## Decision

- One call per message with five tools, `tool_choice: {"type": "auto", "disable_parallel_tool_use": true}`,
  `strict: true` on every tool, and a system prompt that says each message ends in exactly one tool
  (`app/agente.py`, `armar_request`; `app/prompt.py`; `app/tools.py`).
- Adaptive thinking with effort `low`. Without thinking, the tool call came out written as text (spec, R11).
- The model picks the tool and fills its arguments. Every sentence the customer reads comes from
  `app/respuestas.py`, built from fixed templates and the business config. Model text is never sent.
- A response with no `tool_use`, a refusal, an unknown tool or arguments that fail validation becomes
  `SinTool`. The customer gets the fixed re-ask in `MENSAJE_NO_ENTENDIDO` and nothing is retried, because each
  retry costs money (`agente.leer_respuesta`, `turno._aplicar`).
- Arguments are validated by Pydantic with `extra="forbid"`; `telefono` is in no schema, and a test checks it
  (`tests/test_tools.py::test_ninguna_tool_recibe_telefono`).

## Alternatives

- **`tool_choice: any`.** Rejected: the model returns `400`.
- **Let the model write the reply and filter prices afterwards.** Rejected: a filter cannot prove a sentence
  contains no price or promise, while fixed templates cannot contain one.
- **A keyword router in Python.** Rejected (R16): Spanish phrasing such as "listo, gracias" (confirms) and a
  bare "gracias" (does not) is decided by the prompt and measured, not by an `if`.
- **An agent framework.** Rejected: it adds code between a measured prompt and the API.

## Consequences

- Replies are stiff and identical for identical situations. Changing a sentence is a code change in
  `respuestas.py` plus its tests.
- Routing quality lives in the prompt and the tool descriptions. They are measured artifacts: any edit has to
  be followed by a run of the routing battery (R17), which costs money.
- A response without a tool still spends an API call and gives the customer a generic re-ask.
- Three cases of the battery run against the first real configuration fail in every run. They are accepted
  and waiting for a prompt change with its own battery run.
