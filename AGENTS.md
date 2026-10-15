# AGENTS.md

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

## 5. i18n Rules

- Every user-visible string must have a key in `static/locales/en-US.json`.
- Use `uglytags.i18n.t(key, **values)` for Discord messages, labels, and command descriptions.
- Locale values use `str.format`; escape literal TagScript braces as `{{` and `}}`.
- TagScript runtime error messages mirror NotSoBot's English text and live in `tagscript.py`.
- Keep README and other documentation free of emojis.

## 6. UglyTags Conventions

- Python 3.11+; production uses Python 3.14, discord.py 2.7+, and PostgreSQL 18.
- Use `uv sync --locked` for installation. Commit `uv.lock` when dependencies change.
- Scope: NotSoBot's tag commands only, as `/t` (show), `/tag`, the documented `.t`
	chat commands, and `.undo`. Server tags only; no custom commands, DM tags, tag directory, or
	NotSoBot API features.
- Keep Discord commands and forms in `ui.py`, message rendering, components, and
	pagination in `render.py`, component JSON validation in `components.py`, TagScript in
	`tagscript.py`, math in `mathexpr.py`, persistence in `db.py`/`schema.sql`, files in
	`media.py`, and import/export in `transfer.py`.
- Port TagScript behavior from `notsobot.ts` `src/utils/tags/index.ts`. Note intentional
	differences in the README. Never evaluate tag content with Python `eval` or similar.
- Scope every tag operation by guild ID. Recheck ownership and Discord permissions at
	runtime; follow NotSoBot's permission rules documented in the README.
- Outbound HTTP is limited to Discord CDN hosts without redirects. Do not add
	general URL fetching.
- Store tag files in PostgreSQL, enforce `MAX_MEDIA_MB` and 10 files per tag, and never
	trust uploaded filenames.
- Add explicit migration steps before changing an existing database schema. `schema.sql`'s
	version one DDL is followed by numbered, idempotent migration blocks that startup runs
	under an advisory lock; never edit an earlier block, add a new one.
- Keep secrets out of logs, source, and images. Commit `.env.example`, never `.env`.
- Do not install or run Docker on the local development machine. Containers are for
	deployment hosts and CI only.

## 7. Verification

- Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
- Run `uv build` after packaging or locale changes.
- Database tests use a unique temporary schema and require `TEST_DATABASE_URL` for
	a disposable PostgreSQL database. CI runs these tests; disclose local skips.
- Mock Discord HTTP boundaries in unit tests. Live smoke tests require a test server
	and a token supplied outside source control; never request secrets in chat.
- Deployment consumes the GHCR image. Preserve amd64/arm64 publication and least-
	privilege, commit-pinned GitHub Actions.
