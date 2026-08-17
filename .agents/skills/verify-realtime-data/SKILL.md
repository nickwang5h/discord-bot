---
name: verify-realtime-data
description: Verify changing external facts such as model IDs, API behavior, versions, and limits without relying on training memory. Use only when project policy and explicit owner scope permit a bounded live check.
---

# Real-time Fact Checking & API Verification

## Context

Model catalogs, API behavior, versions, prices, limits, and source availability can
change after training. Do not "correct" a user-supplied current fact from memory or
quietly treat it as a typo.

## Authorization boundary

1. Read and follow repository `AGENTS.md` first. A live provider or network check is
   allowed only when the active request has the Owner scope required there.
2. Without that scope, use tracked configuration, lockfiles, cached local catalogs,
   fixtures, and official documentation already supplied by the user. Clearly report
   that live verification was not run and why.
3. Never make a network call silently, inspect private runtime state, or add a
   throwaway dependency/script merely to probe a transient fact.

## Authorized workflow

1. Prefer the repository's existing bounded diagnostic, such as the documented
   strict live health check, over a new ad hoc command.
2. If no project check exists, make at most the smallest read-only request to an
   official endpoint with a short timeout and bounded response. Do not log keys,
   authorization headers, Cookies, private payloads, or full provider responses.
3. Validate the exact user-supplied model/version/limit instead of expanding into a
   provider-wide inventory or unrelated availability survey.
4. Record the source, observation time, result, and whether failure means code error,
   authentication, rate limiting, network failure, or a genuinely absent item.

For example, verify an OpenRouter model against its official catalog only after the
Owner authorizes a bounded live provider check. Ordinary offline tests and local
refactors must not trigger that request.
