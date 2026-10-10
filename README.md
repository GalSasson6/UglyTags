# UglyTags

A Python Discord bot for server tags with TagScript, modeled on
[NotSoBot](https://github.com/NotSoBot/notsobot.ts)'s `tag` commands, backed by
PostgreSQL. Licensed under MIT.

## Commands

All commands are slash commands and work in servers only. Tags belong to the server
they were created in. Tag names are 1-100 characters and are matched
case-insensitively. Management replies are private; `/t` and `/tag random` post
publicly. Every public tag message has a red trash can button that deletes it; the
person who ran the command and server administrators can use it, and it keeps
working after the bot restarts. If a tag's own components fill every button slot, the
delete button is left out.

- `/t name [arguments] [attachment]`: runs a tag. `arguments` fills `{args}`;
  `attachment` is used by `{image}`, `{media}`, and similar functions. Discord does
  not allow `/tag` to take a tag name because it has subcommands, so showing a tag
  uses NotSoBot's `t` alias.
- `/tag create name [content]`: creates a tag. Without `content`, a form opens with
  multi-line content and up to 10 file uploads. If the name is taken, nothing is
  overwritten: the bot asks whether to overwrite it (only offered to the tag's owner
  and server administrators) or create the tag under the first free suggested name:
  `name` suggests `name1`, and a name ending in a number counts up, so `name1`
  suggests `name2`.
- `/tag edit name [content]`: edits a tag, the same way. Owners can edit their own
  tags; server administrators can edit any tag.
- `/tag alias tag name`: creates or repoints an alias that runs another tag.
- `/tag rename tag name`: renames a tag. Owners can rename their own tags; server
  administrators can rename any tag.
- `/tag info name`: shows raw content, owner, ID, uses, dates, and stored files.
- `/tag random [arguments] [content] [name] [user] [attachment]`: runs a random
  matching tag.
- `/tag list server [content] [name] [user]` and `/tag list user [user] [content] [name]`:
  list tags. Filters match case-insensitive substrings.
- `/tag remove tag name`: removes a tag.
- `/tag remove all [content] [name] [user]`: removes matching tags after confirmation,
  with an option to export them first.
- `/tag export [content] [name] [user]`: exports matching tags as JSON.
- `/tag import file`: imports a NotSoBot or UglyTags export.

### Permissions

These follow NotSoBot, except that server administrators can also overwrite tags:

- Only the owner or a member with Administrator (the server owner always has it) can
  edit, rename, or overwrite a tag; this keeps the original owner. Other members are
  offered a different name instead.
- Members with Manage Server or Ban Members can remove anyone's tag after a
  confirmation.
- `/tag remove all` without a user, or for another user, requires Administrator.
  Other members can only remove their own tags.
- `/tag export` without a user, or for another user, requires Manage Server. Other
  members can only export their own tags.
- `/tag import` requires Manage Server.

Removing a tag also removes its aliases, stored files, and stored variables.

## Tag Files

Files uploaded in the create or edit form are stored in PostgreSQL. The bot appends
`{attach:filename}` to the content, and you can move or reuse that reference. Set
`MAX_MEDIA_MB` to a positive whole number. It defaults to 20 MB per file, and each tag
can store at most 10 files. Filenames are sanitized; file contents are not converted
or scanned. When a tag is saved, stored files it no longer references literally are
deleted. Output is checked against the upload limit Discord reports with each command,
and Discord rejects messages whose files exceed it.

## TagScript

TagScript is NotSoBot's `{function:argument|argument}` language. This bot ports
NotSoBot's parser, including nesting, `\` escapes, the 450-iteration budget, and its
limits (10 attachments, 10 embeds, 100 variables, 2 nested tag runs, 15 Discord
requests).

Supported:

- Arguments: `args`, `arg`, `argslen`, `argsafe`, `argssafe`.
- Logic: `if` (`=`, `!=`, `>`, `>=`, `<`, `<=`, `~`), `and`, `or`, `iferror`,
  `includes`, `foreach` (`idx`/`valuex`), `eval`, `evalsilent`, `exit`,
  `exitsilent`, `ignore`, `note`.
- Variables: `set`, `get`, `delete`, `variables`, and persistent `setserver`,
  `getserver`, `deleteserver`, `variablesserver` plus the `user` and `channel`
  equivalents. Persistent variables belong to the tag (an alias shares its target's),
  with at most 5 per scope, keys up to 128 characters, and values up to 16,384.
- Math and random: `math` (a safe subset of mathjs: operators, comparisons, `!`,
  `mod`, common functions and constants), `abs`, `cos`, `sin`, `tan`, `e`, `pi`,
  `max`, `min`, `range`/`random`/`rnd`, `choose`, `uuid` (4 or 7).
- Text: `len`, `lower`, `upper`, `reverse`, `substring`, `indexof`, `repeat`,
  `replace`/`replaceregex` (JavaScript-style regex and `$1` replacements, with a
  time limit), `oneof`, `jsonify`, `traversejson`, `type`, `url`/`urlencode`,
  `newline`, `bracketleft`, `bracketright`, `splitterargument`, `splitterfunction`,
  and all `markup*` functions plus `code`.
- Discord: `user`/`username`/`name`, `usernick`/`nick`, `userid`/`id`,
  `usermention`/`mention`, `usertag`, `userdiscrim`, `useravatar`, `avatar`,
  `userstatus`, `randuser`, `randonline` (and their `id`/`tag` forms), `guild`,
  `guildid`, `guildcount`, `channel`, `channelid`, `channelmention`, `randchannel`
  (and forms), `messagecontent`, `messageuserid`, `messagelastid`, `randmessageid`,
  `json.user`, `json.member`, `json.memberoruser`, `json.channel`, `json.guild`,
  `json.message`, `unix`, `unixs`, `unixsnowflake`, `markuptime`, `prefix` (always
  `/`), `nsfw` (fails outside age-restricted channels).
- Tags: `tag` (by tag ID, same server only), `tagname`, `tagid`, `tagcontent`,
  `tagownerid`, `ismaintag`, `isfromcomponent`, `isfromai` (always `false`),
  `settings`.
- Media and output: `attach`/`attachment`/`file`, `attachspoiler`, `attachtext`,
  `getfilename`, `setfilename`, `image`, `media`, `audio`, `video`, `av`, `iv`,
  `lastattachment`, `embedjson`, `pagejson`, `componentjson`, `componentsontimeout`.

`{attach:...}` accepts a stored filename or a Discord attachment, avatar, or emoji
URL. Expired Discord attachment links are refreshed through Discord's API. The bot
makes no other outbound requests. `{image}` and similar functions use the command's
attachment option, then an argument (URL, user, or custom emoji), and then the last
matching media in the channel's 50 most recent messages.

`{componentjson}` supports NotSoBot's component JSON, including Components V2
containers, sections, text displays, media galleries, files, and separators. A
component's `run` TagScript reruns when it is clicked, with the same variables;
select menu choices appear in `{get:__values}`. Only the user who ran the tag can
press its components unless the tag sets `{settings:ALLOW_COMPONENT_EXECUTIONS_FROM_EVERYONE|true}`.
Components expire after 60 seconds, then run `{componentsontimeout}` if set.
`{pagejson}` output gets previous and next buttons for the user who ran it.

Not supported, and reported as an error when used: NotSoBot API features
(`ai`, `imagine`, `edit`, `iscript`/`mediascript`, `ocr`, `identify`, `transcribe`,
`translate`, `nsfwfilter`, image and YouTube search, weather, locations, and
reminders), general web requests (`request`, `text`/`download`,
`textfromhtml`), `attachvoice`, and global storage (`getglobal`, `setglobal`,
`variablesglobal`). Code-language blocks are not run. Unknown functions are left as
text, as in NotSoBot.

Slash commands have no source or replied-to message, so `replycontent`,
`replyuserid`, `json.messagereply`, and argument-less `messagecontent`,
`messageuserid`, and `json.message` produce nothing. `unix:text` understands
numbers, ISO 8601 dates, `now`, `today`, `tomorrow`, `yesterday`, `in 2 hours`, and
`5 minutes ago` style text, in UTC; NotSoBot's full natural-language parser is not
ported. JSON functions return Discord API-style objects.
Tag output never pings anyone.

A few NotSoBot quirks are deliberately fixed: `\|` inside nested arguments keeps
the pipe, `foreach` restores `valuex` correctly, `finally:` text is not echoed,
the first argument of `includes`, `get`, `or`, and `iferror` is evaluated, and
`iferror`/`evalsilent` do not swallow `exit`.

## Import and Export

Exports use NotSoBot's format (`serverId`, `tags`, `options`, `timestamp`, and tag
objects with `id`, `name`, `content`, `user.id`, `uses`, `created`, and
`reference_tag`). Stored files are included as base64 in an extra `uglytags_files`
field. Exporting an alias also exports the tag it points to. An export larger than
the server's upload limit is refused; use filters to split it.

Import accepts NotSoBot or UglyTags exports up to 50 MB. It keeps names, owners,
uses, dates, aliases, and tag IDs (new IDs are assigned on collision).
`{attach:...}` links to Discord attachments are downloaded into PostgreSQL, refreshing
expired links through Discord, up to 500 links and 200 MB per import. Links that
cannot be downloaded remain as links.
Existing names, aliases whose target was not imported, tags over 4,000 characters,
and malformed entries are skipped and counted.

## Setup

Create an application and bot in the Discord Developer Portal and enable the
privileged Server Members, Presence, and Message Content intents. Install it in a
server with the `bot` and `applications.commands` scopes. Grant View Channel, Send
Messages, Embed Links, Attach Files, and Read Message History. Bots in 100 or more
servers need Discord's approval for privileged intents. Keep the bot token private.

Python 3.11+ and PostgreSQL 18 are supported. Install [uv](https://docs.astral.sh/uv/),
then run `uv sync --locked`. Set `DISCORD_TOKEN` and `DATABASE_URL` in the process
environment, or copy `.env.example` to `.env` and use:

```sh
uv run --env-file .env uglytags
```

For local execution, change the database hostname from `uglytags-db` to the
hostname of your PostgreSQL instance. No Docker installation is required on a
development machine. Set optional `DISCORD_TEST_GUILD_ID` for immediate command
registration in one server; otherwise commands are registered globally and may take
time to propagate. Production should leave this unset. The bot creates its schema on
startup. Back up the database before future schema upgrades.

## Deployment

The deployment Compose file uses a published image, not a local build. On a Docker
host, place `docker-compose.yml` and a configured `.env` together, then run:

```sh
docker compose pull
docker compose up -d
docker compose logs -f uglytags-app
```

Use a strong `POSTGRES_PASSWORD` and the same URL-encoded password in `DATABASE_URL`.
PostgreSQL is not exposed on a host port. The named volume contains tags, files, and
variables; back it up with `pg_dump` and test restoration. Do not run
`docker compose down -v` unless you intend to erase all stored data. Database
major-version upgrades require a PostgreSQL migration, not just an image tag change.

GitHub Actions tests pushes and pull requests, runs CodeQL, and publishes
`ghcr.io/dvir001/uglytags` for `linux/amd64` and `linux/arm64` after successful
checks on pushes to `main` and `v*` tags. Main publishes `latest` and SHA tags;
version tags publish SemVer tags. Set the GHCR package visibility to public after
its first publication, or authenticate the deployment host to pull private images.
Pin `UGLYTAGS_IMAGE` to a release tag or digest for controlled upgrades.

No Discord secrets are needed in CI. Publishing uses GitHub's automatic token with
package-write permission. Enable GitHub Actions, dependency graph, Dependabot
alerts/security updates, secret scanning/push protection, and branch protection for
the CI check in repository settings as available for your account.

## Development

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Database integration tests run when `TEST_DATABASE_URL` points to a dedicated,
disposable PostgreSQL database. They are skipped otherwise. CI supplies this
database as a service. Never point integration tests at production.

User-facing text is centralized in `static/locales/en-US.json`. See `AGENTS.md`
for project conventions. Real `.env` files are ignored; `.env.example`, the
lockfile, and agent instructions are intentionally tracked.
