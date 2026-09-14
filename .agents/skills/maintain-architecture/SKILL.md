---
name: maintain-architecture
description: Maintain Discord Bot architecture, shared runtime, providers, or scheduled delivery. Skip narrow local fixes and documentation-only edits.
---

# Discord Bot Maintenance

Follow the repository-root `AGENTS.md` for authority and verification. Trace the affected entry,
callers, and side effects; consult the relevant part of `arch.md` when ownership or behavior is
unclear. Read the whole architecture only for a change that actually spans it.

## Runtime invariants

- `cogs/` owns Discord interaction and triggers; `core/` owns reusable runtime behavior;
  `scripts/` owns diagnostics without Discord Gateway. Reuse existing code before adding a layer.
- Scheduled and manual delivery share `core.jobs.run_delivery_job()` and its single-flight lock.
  Fetch/generation may retry; `channel.send()` runs once. Post-send reaction, cache, or state
  failures must not resend the message. Update delivery state only after successful sending.
- Use `ask_ai()` for readable answers and `generate_ai()` for machine output. Machine failures
  raise `AIServiceUnavailable`; never parse a friendly error as JSON. Scheduled `ask_ai()` callers
  must use `raise_on_failure=True`.
- Validate machine output types, required fields, URLs, ranges, and length. Titles and URLs come
  from source data, not invented model output. Display constraints need deterministic formatting
  and truncation; reuse `core.utils.create_ai_embed()`.
- Bound AI inputs, output tokens, batches, concurrency, frequency, and total provider timeout.
  Respect cooldowns; shrink 413 payloads and back off on 429. Stop useless remaining batches after
  provider exhaustion. Background work must be cancellable; unload stops owned loops.
- Use `core.storage.JsonStore` and `update()` for atomic read-modify-write. Preserve schema
  compatibility; real data deletion/migration needs its own scope and recovery path.
- Reuse `core.web_fetcher` host/redirect, size, and timeout protections. Never expose credentials
  or private runtime data. Optional warnings alone do not justify installing dependencies.

## Choose verification by the changed behavior

- Delivery/retries: generation may retry, sends remain at most once, concurrent triggers skip,
  and post-send failure does not resend.
- Providers/machine output: failure propagation, cooldown/timeout, and malformed result rejection.
- Storage: use temporary stores for deduplication, migration, or corrupt-input cases.
- Presentation: check the changed formatting and Discord length boundary.
- Cog wiring: check affected extension loading/unloading.

Use existing tests where sufficient; add a focused regression when it protects a real failure.
Follow the root contract for shared validation and live checks. Do not run every category for
unrelated changes, or repeat passing checks without new evidence.

Update only the affected `arch.md` sections when module ownership, dependencies, data flow,
provider order, retry/delivery, storage schema, quotas, configuration, or operational behavior
changes. Finish the requested fix and relevant verification; no extra audit or completion checklist.
