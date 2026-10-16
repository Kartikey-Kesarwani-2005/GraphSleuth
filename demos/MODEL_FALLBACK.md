# Live run: the agent switched models mid-investigation

**Why this file exists.** GraphSleuth's brain is a *real* LLM that picks its own tools. Free tiers
cap tokens **per model, per day**, and an agent loop is token-hungry — so one model's daily budget
can run out *during* an investigation. Rather than dying (or hanging on a retry that will not
succeed for hours), the brain walks a **fallback chain** and keeps going.

This is a real capture of exactly that happening. `openai/gpt-oss-120b` hit its per-day cap on the
first call, the brain switched to `openai/gpt-oss-20b`, and the investigation completed —
no human action, no `--offline`, the same session and the same graph.

```
$ python -m graphsleuth demo --target ACC-9003
```

```
Question: Investigate ACC-9003 — is this account part of a fraud ring?

  [llm] 'openai/gpt-oss-120b' hit its daily cap/unavailable -> switching to 'openai/gpt-oss-20b'
  [1] recall(kind=episodic, query=ACC-9003)
      -> query=ACC-9003, kind=episodic, notes=[5], count=5, open_cases=[4]
  [2] find_shared_links(account_id=ACC-9003)
      -> account=ACC-9003, linked_accounts=4, links=[4], interpretation=shared_devices + shared_beneficiaries…
  [3] open_case(entity_ids=['ACC-9003'], summary=ACC-9003 involved in 6-account mule ring, title=Mule ring investigation: ACC-9003)
      -> case_id=CASE-0005, title=Mule ring investigation: ACC-9003, involves=[1], status=open
```

The model's own report (excerpt) — it chose the tools, nothing here is scripted:

> - **Verdict** — ACC-9003 is part of a confirmed 6-account mule ring (ACC-9002, ACC-9003, ACC-9004, ACC-9100, ACC-9001, ACC-9005).
> - **Evidence chain**
>   1. ACC-9003 –[SHARED_DEVICE:DEV-9001]-> ACC-9002 (score 4.5)
>   2. ACC-9003 –[SHARED_DEVICE:DEV-9001]-> ACC-9001 (score 2.0)
>   3. ACC-9003 –[SHARED_DEVICE:DEV-9002]-> ACC-9004 (score 2.0)
>   …
> - **Tools & queries used** — `recall`, `find_shared_links`, `open_case`.
> - **Actions taken** — `open_case` (CASE-0005).

```
------------------------------------------------------------------------------
EVIDENCE TRAIL
------------------------------------------------------------------------------
 1. recall  [ok]
    args: {'kind': 'episodic', 'query': 'ACC-9003'}
    query: ACC-9003 | kind: episodic | notes: 5 items | count: 5 | open_cases: 4 items
 2. find_shared_links  [ok]
    args: {'account_id': 'ACC-9003'}
    account: ACC-9003 | linked_accounts: 4 | links: 4 items | interpretation: shared_devices + shared_beneficiaries + prior alerts …
 3. open_case  [ok]
    args: {'entity_ids': ['ACC-9003'], 'summary': 'ACC-9003 involved in 6-account mule ring', 'title': 'Mule ring investigation: ACC-9003'}
    case_id: CASE-0005 | title: Mule ring investigation: ACC-9003 | involves: 1 items | status: open
```

> **Note.** This run finished in three tool calls because the smaller `20b` model answered sooner
> than `120b` usually does; the *mechanism* on display is the fallback, not the reasoning depth. A
> fuller live transcript (11 tool calls, end-to-end money path and cluster score) is in
> [`LIVE_RUN.md`](LIVE_RUN.md). Any model here would produce a different tool count — that is the
> point: the agent decides, the graph answers.

## How it works

`.env` (primary first, comma-separated):

```ini
GROQ_MODEL=openai/gpt-oss-120b
LLM_MODELS=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b
```

* A **burst** limit (`429` with *"try again in Xs"*) is handled by the transport's retry/backoff —
  same model, no switch.
* A **daily** cap (`429` mentioning *tokens per day / TPD*) or a **retired** model (`404`) advances
  to the next model in `LLM_MODELS` and retries the *same* request there. `status` prints the chain.
* Only when the whole chain is spent does the run stop — and it then points at `--offline`, the
  explicitly-labelled scripted brain, so the demo is never blocked.

## Pin a model instead

To choose a specific model for a task (no fallback), pass `--model` to `demo`, `pipeline` or `chat`:

```powershell
python -m graphsleuth demo --target ACC-9003 --model qwen/qwen3.8-27b
python -m graphsleuth pipeline --model openai/gpt-oss-20b
```
