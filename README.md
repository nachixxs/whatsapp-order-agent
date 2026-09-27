# whatsapp-order-agent

A WhatsApp agent that takes orders for a small business, writes each confirmed order to a Google Sheet
and hands the conversation to a person in Chatwoot when it needs human judgment. Claude tool use,
FastAPI, SQLite and Google Sheets.

> **Status: design phase.** The specification and the roadmap are written; there is no code yet. This
> README describes what is being built and why, and the [roadmap](#roadmap) below tracks progress
> checkpoint by checkpoint.

## Why a rewrite

This is a from-scratch rewrite of an order bot that was built for a print shop. That bot worked, but it
grew to ~10,300 lines of application code, much of it plumbing around the WhatsApp Cloud API and n8n.
Along the way it collected **120 defenses**: each one prevents a failure that either happened or was
measured (a duplicated order row, a "done!" for an order that was never saved, a model that stopped
calling tools).

The rewrite keeps every defense and drops the plumbing:

- **Chatwoot replaces the custom WhatsApp layer**: webhook signatures, message echoes and notifications to
  staff are now Chatwoot's job.
- **No n8n and no agent framework in the message path.** Each turn is a single Claude call with exactly
  one tool; a framework would add code between a measured prompt and the API.
- **The 120 defenses become 55 numbered rules** in a written spec, each one with a test. Code and tests
  cite them by number (`R4`), and a test checks that every rule is covered.
- **Target: ~2,000 lines of logic**, with a hard budget of 2,500 lines for `app/` enforced at every
  checkpoint.

## What it will do

- **Take orders from free-form messages**, across several messages if needed, and ask for whatever is
  missing (product, material, size, quantity, design, date, name).
- **Show a summary and write the order only after the customer confirms it**, exactly once.
- **Answer common questions** (address, hours, payment methods, how to send files) from the business
  config, with fixed text.
- **Hand off to a person** when the customer asks for one, asks for a price or a deadline, or asks for
  something the business doesn't have. The conversation goes to a human in Chatwoot with an internal note
  summarizing what the bot understood.
- **Never quote prices, deadlines or deposits.** That is the person's job.

| The customer writes | The agent |
|---|---|
| "necesito 500 tarjetas" | `registrar_pedido`, then asks for the next missing field |
| "¿a qué hora abren?" | `consulta_general(horarios)`: fixed text built from the config, holidays included |
| "¿cuánto sale?" | `derivar_a_asesor(plazo_o_precio)`: hands off, without a number |
| "sí" (in reply to the summary) | `confirmar_pedido(acepta=true)`: writes one row to the sheet |

## How it works

```
Customer ──WhatsApp──▶ Meta ──▶ Chatwoot ──webhook──▶ FastAPI
                                   ▲                   │
                                   └──Chatwoot API─────┤  reply, internal note, handoff
                                                       ├──▶ Claude API (one tool per turn)
                                                       ├──▶ SQLite (history, order, dedup)
                                                       └──▶ Google Sheets (one row per order)
```

## Engineering decisions

- **Tool use as the router, always.** `tool_choice: any` with parallel tool use disabled: with `auto`, the
  model sometimes answered in prose and the customer got "I didn't understand". Five tools, one decision
  per turn, and no keyword `if` anywhere.
- **The customer only reads text composed in Python.** The model picks a tool and fills its arguments;
  replies are built from fixed templates and the business config. Free text from the model never reaches
  the customer.
- **Atomic confirmation.** "yes" and "ok" sent a second apart must write one row, not two. The order is
  taken for confirmation atomically in SQLite and carries a generation number, so a message that read
  the order before it was taken can't resurrect it. If the sheet write fails, the customer is told so
  and the order goes back to pending.
- **Its own history, with markers instead of prose.** Chatwoot stores the full conversation, but the
  agent keeps its own history in SQLite, where the bot's turns are markers like `[dato_faltante: material]`.
  Measured on the previous bot: with prose, the model called the tool 0 times out of 3; with markers,
  3 out of 3.
- **The prompt is a measured artifact.** The system prompt and the tool descriptions are kept word for
  word from the previous bot and are only changed together with a run of the routing test suite:
  reordering its clauses dropped one routing case from 6/6 to 3/6.
- **One clock.** Every business decision uses the business time zone, never the server's clock. Holidays
  come from a list in the config, and the bot warns when that list is about to run out.
- **Public code, private data.** This repo ships a fictional business (`config/negocio.ejemplo.json`).
  The real configuration lives in a private deploy repo, and `gitleaks` runs in CI.

## Tech stack

Python 3.14 · FastAPI · Pydantic 2 · Claude API (`anthropic`, tool use) · SQLite · Google Sheets
(`gspread`) · Chatwoot (`httpx`) · Docker Compose · pytest · GitHub Actions

## Roadmap

| Checkpoint | Delivers |
|---|---|
| 1 | Local Chatwoot and a webhook skeleton; open questions about Chatwoot answered with evidence |
| 2 | The agent: fictional config, prompt built from config, five tools, SQLite history |
| 3 | Orders: atomic confirmation, Google Sheets, files, the "yes" race |
| 4 | First contact, business hours with holidays, handoff in Chatwoot |
| 5 | Routing parity with the previous bot, measured with the real config |
