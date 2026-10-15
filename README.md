# UglyTags

A Python Discord bot for server tags with TagScript, modeled on
[NotSoBot](https://github.com/NotSoBot/notsobot.ts)'s `tag` commands, backed by
PostgreSQL. Licensed under MIT.

## Commands

Commands work in servers only, using slash commands, `.t` chat commands, and a message Apps shortcut.
Tags belong to the server they were created in. Tag names are 1-100 characters and are matched
case-insensitively. Slash management replies are private; chat management replies and
tags run with `/t`, `/tag random`, or `.t` post publicly. Every public tag message has
a red trash can button that deletes it; the
person who ran the command and server administrators can use it, and it keeps
working after the bot restarts. If a tag's own components fill every button slot, the
delete button is left out.

- `/t name [arguments] [attachment]`: runs a tag. `arguments` fills `{args}`;
  `attachment` is used by `{image}`, `{media}`, and similar functions. Discord does
  not allow `/tag` to take a tag name because it has subcommands, so showing a tag
  uses NotSoBot's `t` alias.
- `.t name [arguments]` and `.tag name [arguments]`: run a tag from chat and reply to
  the command message without pinging its author. Quote names containing spaces. The
  original `.t show`/`.tag show` aliases also work.
- When a chat or slash command names a tag that does not exist, the reply suggests up
  to three similarly spelled tag names from the server: "Unknown Tag. Did you mean
  `hello`?" NotSoBot only says "Unknown Tag". Matching uses PostgreSQL's
  `pg_trgm` extension, so very short or symbol-only names may get no suggestions.
- `.t create`, `.t edit`, `.t alias`, `.t rename`, `.t info`, `.t list`, `.t random`,
  and `.t remove`: provide the original bot's core chat management commands. `.t add`,
  `.t delete`, `.t owner`, and `.t raw` remain aliases. Create, edit, and remove always
  ask for confirmation. Create and edit store files attached to the command message.
  Reply with `.t add name` or `.t create name` (without content or command attachments)
  to create a tag from the replied message's text, attachments, and bot embed media
  links (for a forward, the original message's).
  Unlike NotSoBot, which keeps only the first link, the full text is copied, unless the
  message has attachments, in which case its text is treated as a caption and skipped.
  Attachments are stored with the tag; links remain links rather than being downloaded
  from external hosts.
  Explicit content or command attachments take priority over the reply. A taken name
  gets the same choices as `/tag create`: overwrite (when allowed), create it as the
  next free numbered name (from 2, such as `name2`), or enter a custom name, which is
  checked again. The create confirmation also has a Rename button.
  Use the slash commands for forms, filters, imports, exports, groups,
  statistics, and pruning.
- `.t search name`: list the server's tags whose names contain `name` (case-insensitive),
  matching `/tag list server name:`. Quote names containing spaces. Without a name it
  behaves like `.t list`. See below for similar-name results.
- Editing a `.t` or `.tag` chat command within 15 minutes of sending it
  updates the bot's earlier reply instead of sending a new one, and editing it into
  something that is not a command, or deleting it, removes the reply, like NotSoBot.
  If the new reply cannot replace the old one (for example a Components V2 reply
  becoming a plain one), the old reply is deleted and a new one is sent. Re-running an
  edited command counts as another use of the tag. Replies are tracked in memory, so
  edits made after a restart are not matched to their old replies.
- `.undo [amount]`: deletes the bot's replies to your last `amount` commands in this
  channel, counting both chat commands and public slash command tag output (`/t`,
  `/tag random`, and tag reply). The amount is 1 to 5, default 1, and only commands
  from the last 15 minutes since the last restart count. When the bot has Manage
  Messages it also deletes your chat command messages and the `.undo` message itself,
  and a short notice saying how many commands and slash interactions were deleted
  disappears after one second; without it the notice replies to `.undo`. Editing an `.undo` message runs
  it again. Unlike NotSoBot it does not reach commands older than 15 minutes.
- **Apps > tag reply**: right-click a message (or long-press on mobile)
  and choose this shortcut (prefixed with a speech-bubble icon). A private form offers your 25 most recently used tags in
  this server, newest first, with optional arguments and one attachment. Submitting
  posts the tag publicly, followed by an author mention on a smaller subtext line
  beneath the content and a compact reply card
  with the original author's name, avatar, and linked message excerpt, like CopyCat's
  reply shortcut. Messages are still sent as UglyTags, not as your profile.
  Only that author can be pinged; bots are not pinged.
  The usual TagScript, usage counting, and trash can controls still apply. History
  uses the retained one-year use log, including tags run through aliases, groups,
  and nested tags; previews do not enter history. Deleted tags are left out, and
  renamed tags appear with their current names. Both you and the bot need permission
  to view the channel and send messages there.
  The reply card stays on every pagination page and component update; tags that
  leave no room for it report an error and can still be run with `/t`.
- `/tag create name [content]`: creates a tag. Without `content`, a form opens with
  multi-line content and up to 10 file uploads. Creating or editing a tag always
  requires confirmation before saving, including after submitting a form. If the name
  is taken, nothing is overwritten: the bot asks whether to overwrite it (only offered
  to the tag's owner and server administrators) or create the tag under the first free suggested name:
  `name` suggests `name1`, and a name ending in a number counts up, so `name1`
  suggests `name2`.
- `/tag edit name [content]`: edits a tag, the same way. Owners can edit their own
  tags; server administrators can edit any tag.
- `/tag alias tag name`: creates or repoints an alias that runs another tag.
- `/tag rename tag name`: renames a tag. Owners can rename their own tags; server
  administrators can rename any tag.
- `/tag info name [arguments] [attachment]`: privately previews the tag's output
  together with its raw content, owner, ID, uses, dates, and stored files. A preview
  does not count as a use and does not save persistent variables; a group previews
  one of its tags.
- `/tag random [arguments] [content] [name] [user] [attachment]`: runs a random
  matching tag.
- `/tag list server [content] [name] [user]` and `/tag list user [user] [content] [name]`:
  list tags. Filters match case-insensitive substrings.
  With a name filter (here or in `.t search`), the results have an **Include similar
  names** button that adds tags with similarly spelled names after the substring
  matches, and a search that finds nothing shows the similar names instead. NotSoBot
  has neither.
- `/tag remove tag name`: removes a tag after confirmation, even for its owner.
- `/tag remove all [content] [name] [user]`: removes matching tags after confirmation,
  with an option to export them first.
- `/tag undo [amount]`: the slash version of `.undo`, answered privately. It deletes
  the bot's replies to your last commands in this channel, but cannot delete chat
  command messages without Manage Messages, the same as `.undo`.
- `/tag export [content] [name] [user]`: exports matching tags as JSON.
- `/tag import file`: imports a NotSoBot or UglyTags export. To move your tags to
  another server, run `/tag export` in the old server and `/tag import` in the new one.
- `/tag group create name tag`, `/tag group add group tag`, and
  `/tag group remove group tag`: manage tag groups. A group is a tag whose name runs a
  random tag from the group, so `/t cats` posts one of the group's tags. Groups share
  names with tags and work with `/tag info` (which lists the group's tags), `rename`,
  `alias`, `remove`, `random`, and `{tag:ID}`. The group's owner and server
  administrators can add or remove any of the server's tags, up to 100 per group.
  Groups cannot contain other groups or have content of their own, and a deleted tag
  leaves every group it was in.
- `/tag stats overview [period]`: tag and use totals, uses in the period, storage,
  the 10 most used tags, the 5 members who ran the most tags, and an unused-tags
  summary (never used, used at most once, and not used in 30, 90, or 365+ days) to
  size up a prune.
- `/tag stats tag name [period]`: one tag's uses (all time and in the period), last
  use, creation, owner, storage, and top members.
- `/tag stats member [member] [period]`: a member's tag count, uses of their tags,
  tags they ran, storage, and the tags they ran most.
- `/tag stats ranking [sort] [period] [owner] [unused_days] [max_uses] [min_age_days]`:
  a paged ranking (default: most used) by most used, least used, recently used,
  longest unused, newest, or oldest. Each row shows the tag, owner, uses, last use,
  creation date, and size. The filters work exactly like `/tag prune`'s, so running it
  with the same values previews what a prune would match.
- `/tag stats storage`: the server's storage use (files, tag text, and stored
  variables), the largest tags, and the members using the most storage. Sizes use
  1 MB = 1,000,000 bytes.
- `/tag prune [unused_days] [max_uses] [least_used] [owner] [min_age_days]`: removes
  unused tags. `unused_days` matches tags not used in that many days (never-used tags
  count), `max_uses` matches tags with fewer total uses, `owner` limits it to one
  member's tags, and `least_used` keeps only that many of the least-used matches.
  Give at least one of `unused_days`, `max_uses`, or `least_used`; combined criteria
  must all match. Tags created in the last `min_age_days` (default 7) are never
  pruned. Before anything is removed, it lists every matching tag in the same detailed
  format as the ranking, across as many pages as needed, with "Remove these tags",
  Cancel, and Export buttons. Removing deletes exactly the listed tags; nothing
  happens if no button is pressed within five minutes. Aliases and groups can be
  pruned like any tag, and pruning a tag also removes its aliases.

Stats are visible to everyone as private replies; the period is the last 7 days, 30
days, year, or all time (the default). Running an alias also counts as a use of its
target, and running a group also counts for the tag it picked, so tags used only
through aliases or groups are not treated as unused. Totals count each run once. The
bot logs each use (tag, member, and time) for period and per-member statistics and
deletes log entries after one year; all-time totals and last-used times are kept on
the tag. Usage from before this log existed, including imported NotSoBot counts,
appears only in all-time totals.

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
- `/tag import` imports every tag in the file for members with Manage Server. Other
  members can import too, but only tags they own in the file; the rest are skipped
  and counted.
- `/tag prune` requires Manage Server. `/tag stats` is open to everyone.

Removing a tag also removes its aliases, stored files, stored variables, and use log.

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
  `/`), `nsfw` (an opt-in lock: the tag fails outside age-restricted channels),
  `nsfwfilter` (outputs its text unfiltered, up to 2,000 characters).
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
`translate`, image and YouTube search, weather, locations, and
reminders), general web requests (`request`, `text`/`download`,
`textfromhtml`), `attachvoice`, and global storage (`getglobal`, `setglobal`,
`variablesglobal`). Code-language blocks are not run. Unknown functions are left as
text, as in NotSoBot. NotSoBot's output filter (which replaces flagged or very long
output with "i love cats") is not ported; tag output is never censored.

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
field, and groups list their tag IDs in an extra `uglytags_group_members` field.
Exporting an alias or group also exports the tags it points to. An export larger than
the server's upload limit is refused; use filters to split it.

Import accepts NotSoBot or UglyTags exports up to `MAX_IMPORT_MB` (default 50 MB). It
keeps names, owners, uses, dates, aliases, groups, and tag IDs (new IDs are assigned
on collision). `{attach:...}` links to Discord attachments are downloaded into
PostgreSQL four at a time, refreshing expired links through Discord, up to
`MAX_IMPORT_DOWNLOADS` links (default 500) and `MAX_IMPORT_DOWNLOAD_MB` of data
(default 200 MB) per import. Links over those limits or that cannot be downloaded
remain as links. Downloaded files are held in memory until the import is saved, so
size `MAX_IMPORT_DOWNLOAD_MB` to the host's free memory. For a large one-time
migration, raise these in `.env`, restart the bot, import, and then lower them again.
The uploaded file is also subject to your own Discord upload limit. If an import
takes longer than Discord's 15-minute reply window, it still completes and its result
is written to the bot log.
Existing names, aliases whose target was not imported, tags over 4,000 characters,
and malformed entries are skipped and counted.

## Setup

Create an application and bot in the Discord Developer Portal and enable the
privileged Server Members, Presence, and Message Content intents. Install it in a
server with the `bot` and `applications.commands` scopes. Grant View Channel, Send
Messages, Embed Links, Attach Files, and Read Message History. Bots in 100 or more
servers need Discord's approval for privileged intents. Keep the bot token private.

Python 3.11+ and PostgreSQL 18 are supported. The bot creates PostgreSQL's bundled
`pg_trgm` extension on startup, so its database user must own the database or have
CREATE on both the database and its `public` schema. Install [uv](https://docs.astral.sh/uv/), then run `uv sync --locked`. Set `DISCORD_TOKEN` and `DATABASE_URL` in the process
environment, or copy `.env.example` to `.env` and use:

```sh
uv run --env-file .env uglytags
```

For local execution, change the database hostname from `uglytags-db` to the
hostname of your PostgreSQL instance. No Docker installation is required on a
development machine. Set optional `DISCORD_TEST_GUILD_ID` for immediate command
registration in one server; otherwise commands are registered globally and may take
time to propagate. Production should leave this unset. The bot creates its schema on
startup and applies migrations automatically, such as adding tag groups to a
version one database. Back up the database before upgrading.

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
