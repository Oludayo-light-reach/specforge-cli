# `spec team watch` — slash commands

While `spec team watch` is running, the CLI reads **stdin** in parallel with the SSE stream. Lines starting with `/` are treated as commands (they are not echoed as chat). Commands are **disabled** with `--no-commands`.

**Requirements**

- **Signed in** (`spec login`) for anything that talks to Cloud (`/flag`, `/turn`, `/full`).
- **`/pair`** only works in `spec team watch` (paired Q/A mode).
- **`/push`** / **`/push@…`** need your **cwd inside a Spec bundle** so the CLI can write `.spec/team-push-requests.yaml` (no Cloud call; uses `spec login` identity when available for `from_handle`).
- **`/turn`** and **`/full`** need the same Cloud client as `/flag`. They call  
  `GET /api/projects/{id}/prompt-events?session_id=<exact>&since_id=<cursor>&limit=1000`  
  repeatedly (paginated) until the session is exhausted.  
  **Pager:** by default the CLI runs **`less`** (or **`PAGER`**, or **`SPEC_TEAM_WATCH_PAGER`** if set) with the thread text on stdin; **press q** to leave the pager and return to the live stream. Live lines that arrive while the pager is open are suppressed (counter + `/replay` hint on return).

**How results look on screen**

The notifier prints a **`spec>`** line (Rich). The first character is a **glyph** by result kind:

| Glyph | Kind | Used for |
|------|------|----------|
| `·` | `info` | Help text, status, search hits, “nothing to show”, usage hints |
| `✓` | `ok` | `/focus`, `/mute`, `/unmute`, `/critic`, successful `/flag` |
| `✗` | `error` | Unknown command, bad args, network/API failures, ambiguous session |
| `≡` | `summarize` | `/summarize` (inline). **`/turn`** and **`/full`** normally open your **system pager** (`less`, or `PAGER` / `SPEC_TEAM_WATCH_PAGER`) instead: live feed printing pauses until you press **q** in the pager, then streaming resumes. If no pager is found, the thread body prints inline as `≡` like `/summarize`. |

---

## Command reference

### `/help` — list commands

**Aliases:** `/h`, `/?`

**Input:** none.

**Output:** Fixed multi-line list (same idea as this doc, shorter).

---

### `/summarize <n>h` or `<n>m` — context dump for an agent in this terminal

**Alias:** `/summary`

**Input:** One window like `2h`, `45m`, `30 m` (see `/help` for format).

**Output (`≡`):** A framed block:

1. Header: `[spec summarize request — past <window>]`
2. Separator line (`=…`)
3. For each non-presence event in the buffer whose time falls in the window: one header line  
   `{author} · {HH:MM:SS} · {ROLE} · {source}{/model if assistant} · {branch}{ · bundle}`  
   then indented body lines from `text` or `summary`, or `  (no body)`
4. Footer asking the in-terminal agent to synthesise themes and next actions.

**API:** none (buffer only).

---

### `/flag <event_id> <kind> [note…]` — post a flag on a prompt event

**Input:** Numeric event id, one of `warning` | `question` | `block` | `ack`, optional free-form note (everything after the kind token).

**Output:**  
- Success (`✓`): `flagged #<id> as <kind>` plus optional ` — <note>`.  
- Errors (`✗`): not wired (no API), unknown kind, bad event id, event id not in buffer, or API error message.

**API:** `POST` flag via `CloudClient`; project id resolved from the in-memory buffer.

---

### `/push <handle> [message…]` / `/push@handle [message…]` — git-push handoff (YAML)

**Input:** Teammate **Spec handle** (GitHub-style); optional message words after the handle.

**Examples:** `/push jc`, `/push@jc need your WIP for integration`.

**Output:**  
- `✓` confirmation with path to `.spec/team-push-requests.yaml`.  
- `✗` if cwd is **not** inside a bundle (common when `spec team watch` runs from `$HOME` — `cd` to the bundle first).

**Effect:** Appends a time-bounded row locally. **`spec watch`** merges non-expired rows into `.spec/team-presence.json` (`push_requests`) and `.spec/team-editing-brief.md` on the next mirror tick (typically within **30 seconds**).

**API:** none (filesystem only). `from_handle` / display name are taken from `spec login` when present.

---

### `/focus <handle>` or `/focus off` — show one teammate only

**Input:** Handle (with or without `@`), or `off`.

**Output:**  
- `✓` `focus → @<handle>. …` or `focus cleared.`  
- `✗` usage if args missing (unless focus already set — then `·` explains current focus).  
- `·` if you ask for status-style “who is focused” without clearing.

**Effect:** Filters which **live** events print; does not change Cloud.

---

### `/mute <handle>` / `/unmute <handle>` — hide one teammate’s stream

**Output:**  
- `✓` `muted @<handle>. …` or `@{handle} unmuted.`  
- `✗` usage if handle missing.  
- `·` `@{handle} was not muted.` for redundant `/unmute`.

**Effect:** Additive mutes; composes with `/focus` (focus wins for visibility logic when both apply — see code: mute can still hide a focused user).

---

### `/replay <n>h` or `<n>m` — re-print buffered events

**Input:** Same window grammar as `/summarize`.

**Output:**  
- `·` `replaying N event(s) from the last <window>…` then each selected event is passed through **`Notifier.show()`** again (same rendering as live: badges, critic, filters, etc.).  
- `·` `nothing to replay in that window.` if empty.

**API:** none.

---

### `/search <term>` — grep the in-memory buffer

**Aliases:** `/grep`, `/find`

**Input:** Non-empty search string (can include spaces — uses the raw tail after `/search` in practice for the needle; see implementation).

**Output (`·`):** Up to **25** matches, newest first:  
`N match(es) for '<term>' (newest first):`  
then lines: `  #<id>  <time>  <role>  <author>  <snippet>`  
Snippet is a short extract around the first body hit, or `no matches…`.

**API:** none.

---

### `/pair` — force-print the pending user+assistant pair

**Input:** none.

**Output:**  
- Delegates to team watch Q/A flush: either prints the **paired reply** block (same as idle flush) or `·` messages like nothing pending, no assistant rows yet, etc.  
- `✗` `/pair only works in 'spec team watch'…` if not in that mode.

**API:** May read Cloud tail inside the watch handler (existing coalesce behaviour).

---

### `/turn [<session-chip>]` — **only the latest turn** in that session

**Meaning:** The **last user message** in the session (by event `id`), plus every **assistant** row after it until the next user — merged into one logical reply (streaming chunks combined). That is exactly “last prompt + full AI answer to *that* prompt”, not the whole thread.

**Input:** Optional **prefix** of `session_id` (must match **exactly one** `(project_id, session_id)` among user/assistant/error rows in the **in-memory buffer**). If omitted, uses **`notifier.last_turn_digest()`** after **`● turn complete`** (digest mode).

**Cloud:** Paginated fetch for that `session_id` (see Requirements). If the session exceeds the configured row cap (default **120 000**, override with env **`SPEC_LIVE_THREAD_MAX_ROWS`**), a **`[note]`** line is prepended: earliest events may be missing and the last turn may be incomplete.

**Output:** Opens in your **system pager** (see Requirements): a banner at the top of the buffer reminds you **Press q** to return to the live feed. The live stream does **not** print underneath while the pager runs (missed lines are counted; a hint suggests `/replay` if needed). If no pager executable is found, the same text is printed inline as a `≡` block.

**Errors / info:**  
- `✗` Cloud client missing; fetch failed; no buffer match; ambiguous chip (**no** HTTP call on ambiguous).  
- `·` No user rows in Cloud for that session; no assistant after latest user (still streaming).

```text
── /turn · session <8-char>… · user #<user_id> ──

USER
<full user text>

ASSISTANT (#<id>–#<id>)
<full merged assistant text + summary per merge rules>

ERROR (#<id>) — <model>     # when agent error rows exist in the turn window

TOOL RUNS (N)    # only if tool_calls present
  · <one line per tool, up to 50>
  · …+K more tools   # if more than 50
```

---

### `/full [<session-chip>]` — **entire session** (all user→assistant turns)

**Meaning:** Every **user** row in that `session_id`, in order, each paired with the **assistant** and **error** rows that follow until the next user — same merge rules as `/turn`, but **all** turns, not just the last one.

**Input:** Same session resolution as `/turn`.

**Cloud:** Same paginated `session_id` fetch as `/turn`. If the fetch hits the row cap, a **`[note]`** line is prepended: older rows may be missing. Independently, the **printed** output is capped (default **~900 000** characters; override with **`SPEC_LIVE_THREAD_PRINT_MAX_CHARS`**) with a truncation footer.

**Output:** Same pager behaviour as `/turn` (banner + **q** to return). Multiple turns are concatenated in one pager session.

**Errors:** Same family as `/turn` (`✗` / `·`), except `/full` always continues with a note when the row cap is hit instead of aborting.

---

### `/critic on` or `/critic off` — toggle auto-critic

**Output:**  
- `✓` `auto-critic enabled.` / `disabled.`  
- `·` current state and usage if args invalid or missing.

**Effect:** Only affects **future** rendering paths that consult `WatchState.critic_enabled`.

---

### `/status` — visibility + who was active recently

**Alias:** `/who`

**Input:** none.

**Output (`·`):**  

1. Optional first line: **receiver brief** from team watch (SSE/layout digest).  
2. **visibility:** line — all teammates, or `/focus`, or `/mute` list.  
3. Either `no activity yet` / `no non-presence events`, or a table:  
   `active teammates (last seen, source, bundle):`  
   then rows: `  <author>  <source>  <age>  · <bundle>`.

**API:** none.

---

## Aliases summary

| Command | Alias |
|---------|--------|
| `/help` | `/h`, `/?` |
| `/summarize` | `/summary` |
| `/search` | `/grep`, `/find` |
| `/status` | `/who` |

---

## Typing unknown commands

**`/something`** → `✗` `unknown command: /something. Try /help for the list.`

If a handler raises unexpectedly, you get `✗` `/<name> failed: …` and the watcher keeps running.
