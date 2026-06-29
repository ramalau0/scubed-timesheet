# Code Review: Auto-select Client/Project Feature

**Reviewer:** MMdebuka
**Date:** 29/06/2026
**Branch:** master
**Commits reviewed:** af2330c (implementation of PLAN-auto-select-client-project.md)

---

## Summary

The implementation partially fulfils the plan. The `discover_all` command, catalog crawling, and `create` multi-project split all work correctly. However, there are **two critical defects** that break the end-to-end automation promise — the bot cannot run without a human stepping in to manually configure `project_mappings.json`.

---

## Critical Defects

### Bug 1 — `discover_all` generates an empty mappings file (requires manual editing)

**File:** `timesheet_bot.py`, lines 636–648
**Function:** `discover_all`

**What it does:**
```python
mappings = {
    "default_client_id": default_client.get("id"),
    "mappings": []          # ← always empty
}
```

`discover_all` creates `project_mappings.json` with `"mappings": []`. The user is shown a hint message but must manually open the file and add entries.

**Impact:** The automation is not end-to-end. Any user without Claude Code, Python knowledge, or JSON editing experience is blocked. The whole point of `discover_all` is a one-command setup — this breaks that.

**What must be fixed:** After fetching the catalog, `discover_all` must auto-derive glob patterns from the project names it just downloaded and write them into `mappings`. For example:
- `"DevOps Project - GoTurbo FTTH"` → strip prefix → `"goturbo*"` → `client_id: 2859, project_id: 5092`
- `"DevOps Project - DaaS Platform"` → `"daas*"` → `client_id: 429, project_id: 5094`
- `"DevOps Project - eBilling Modernisation"` → `"ebilling*"` → `client_id: 2859, project_id: 5098`

Common prefixes to strip: `"DevOps Project - "`, `"TES - "`, `"MWS "`. Take the first word of the remainder, lowercase, strip non-alphanumeric, append `*`.

No manual editing should ever be required. The generated file can still be edited by advanced users afterwards, but it must work out of the box.

---

### Bug 2 — `resolve_project_ids` ignores `project_id` from mappings

**File:** `timesheet_bot.py`, lines 73–91
**Function:** `resolve_project_ids`

**What it does:**
```python
project = client["projects"][0]   # ← always uses the first project, ignores mapping
```

Even if a mapping entry specifies `"project_id": 5092`, this function always picks `client["projects"][0]` — the first project alphabetically returned by the API. The `project_id` field in `project_mappings.json` is silently ignored.

**Impact:** All timesheet entries for a given client are logged against the wrong project (whichever happens to be first in the API response). For the eNetworks client this means GoTurbo hours could be logged against eBilling Modernisation.

**What must be fixed:** After resolving the client, check if the matched mapping (or the default) includes a `project_id`. If so, find that specific project in `client["projects"]` by ID. Fall back to `client["projects"][0]` only if no `project_id` is specified or the ID is not found.

```
if project_id is specified in mapping:
    project = find project by that ID in client["projects"]
    fallback to client["projects"][0] if not found
else:
    project = client["projects"][0]
```

Also: the `"default_project_id"` key in the mappings root is defined in the plan but `resolve_project_ids` does not read it. It only reads `"default_client_id"`. The default project fallback is missing entirely.

---

## Minor Issues

### Issue 3 — `discover_all` does not handle HTML entities in project names

**File:** `timesheet_bot.py`, lines 629–632

The catalog printout (and the catalog JSON file) contains raw HTML entities from the API response:
- `"Cyber Security &amp; Network Support"` (should be `"Cyber Security & Network Support"`)
- `"TES - PR Komprise POC&#39;s"` (should be `"TES - PR Komprise POC's"`)

These entities are stored as-is in `client_catalog.json`. When pattern derivation is added (Bug 1 fix), the `&amp;` and similar entities in names must be decoded before deriving glob patterns, otherwise patterns like `"cyber"` will work but anything derived from an entity-containing name will be wrong.

**Fix:** Run project and client names through `html.unescape()` when saving to the catalog and when printing.

---

### Issue 4 — `project_mappings.json` is not in `.gitignore`

**File:** `.gitignore`

`client_catalog.json` is correctly excluded (contains employee/entity IDs). However `project_mappings.json` may also contain internal IDs and should be excluded for the same reason — or at minimum a note should be added to `.env.example` / README explaining whether it should be committed.

---

## What Works Correctly

- `discover_all` crawler: fetches all clients, projects, activities, and designations non-interactively via the browser API. No `input()` calls. ✅
- `create_week` multi-project split: correctly groups repos by `(client_id, project_id)` and splits `HOURS_PER_DAY` evenly across distinct projects. ✅
- `split_hours` rounding: last bucket absorbs remainder so hours always sum to exactly `HOURS_PER_DAY`. ✅
- `get_git_work_for_date`: git commit history enrichment works alongside Claude CLI history. ✅
- Fallback to default client when no repos match: correct. ✅
- `build_entry` now accepts explicit IDs rather than module globals. ✅

---

## Priority Order for Fixes

| # | Defect | Priority | Effort |
|---|--------|----------|--------|
| 1 | `discover_all` generates empty mappings | 🔴 Critical — blocks automation | Low |
| 2 | `resolve_project_ids` ignores `project_id` | 🔴 Critical — wrong project logged | Low |
| 3 | HTML entities in catalog names | 🟡 High — breaks pattern matching | Low |
| 4 | `project_mappings.json` not in `.gitignore` | 🟢 Low — hygiene | Trivial |
