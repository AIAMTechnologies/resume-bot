# Resume Bot — rules for any Claude session in this folder

**Read `docs/HANDOFF.md` first.** Then `docs/APPLY_PLAYBOOK.md`.

## Never touch the database from a VM / mounted folder (Cowork, Dispatch, remote sandboxes)
`data/resumebot.db` is a live SQLite database in WAL mode, owned by the running bot process.
Opening it through a virtual-machine or FUSE mount of this folder (paths like `/sessions/.../mnt/...`, stray
`.fuse_hidden*` files appearing) **corrupts it** — the mount does not share SQLite's lock/shared-memory files.
This happened on 2026-10-01 18:46: a Cowork session "bulk released" review jobs with direct SQL and the bot died with
"database disk image is malformed". Recovery took `.recover` plus a row-by-row repair from a backup.

- From a VM/mounted session: do **not** run `sqlite3`, Python `sqlite3`/SQLAlchemy, or copy `resumebot.db*`.
  Change state only through the bot's dashboard API on the Mac (`http://127.0.0.1:8765`: `POST /review/{id}`
  `action=approve|skip|answer`, `POST /control/pause|resume/{source}`, `POST /control/global-pause`), or ask the user.
- Only a session running directly on the Mac may use `sqlite3` on the file, and even then prefer the API for writes.
- A backup made by copying only `resumebot.db` (without `-wal`) silently misses the latest changes.

## One session at a time
Only one Claude session drives the bot. Don't start a second copy of the bot, don't restart it, and don't edit
`config/` or the database if another session is already working here — check `docs/HANDOFF.md` and ask the user.

## Standing rules from Ammar
- Never apply under $100,000; never apply to Cohere; LinkedIn is assist-only (the bot never signs in).
- Human checks (Greenhouse emailed codes, CAPTCHAs) are completed by Ammar, never by the bot or by Claude.
- Commit as you go; don't push without asking.
