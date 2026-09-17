# Codex Session Cleaner

[简体中文](README.md) | **English**

A local Windows tool for managing and cleaning up Codex sessions. Use your browser to inspect storage usage, preview conversations, organize attached records, and delete sessions you no longer need.

The service listens only on `127.0.0.1` and does not upload conversation content. Normal use requires no Node.js, frontend dependencies, or additional database installation.

## Features

- **Storage overview**: Sort by size or last activity, search records, filter by project, and navigate project groups.
- **Session relationships**: Display subagent tasks and safety reviews under their parent sessions when an explicit relationship is available. Attached records are collapsed by default.
- **Content previews**: Click a title to view messages, replies, and related files. Previews support incremental loading, a resizable pane, and clearing the current preview.
- **Bulk cleanup**: Select individual records, all matching records, project groups, or groups of unlinked records.
- **Missing-file cleanup**: Filter for records whose files are all missing and remove the remaining session records through the official CLI.
- **Database maintenance**: Reclaim unused database space and optionally remove runtime logs according to a retention period.
- **Chinese / English**: The initial language follows your browser preference. Manual choices are remembered.

## Requirements

- Windows.
- Python 3.10 or newer, with `python` or `py` available on your PATH.
- Existing local Codex session data.
- A compatible Codex CLI for session deletion. The tool attempts to find it automatically.

**The current deletion compatibility baseline is `codex-cli 0.154.0-alpha.6.2`.** Session deletion is disabled when the locking protocol of another CLI version has not been verified. This does not mean your data is damaged. Python testing has primarily used Windows with Python 3.13.

## Quick start

Download or clone the project, then double-click `start.cmd`. The tool opens your browser and scans the local Codex data directory.

Alternatively, run this from the project directory:

```powershell
python app.py
```

The default address is `http://127.0.0.1:8765`. If that port is unavailable, the tool uses another available port. Check the startup window for the actual address. Keep that window open; closing it stops the service.

### Choose a data directory or port

```powershell
# Use a specific Codex data directory
python app.py --home "D:\CodexData"

# Specify the official CLI executable
python app.py --cli "C:\path\to\codex.exe"

# Choose a port without opening the browser automatically
python app.py --port 8766 --no-browser
```

| Option | Description | Default |
| --- | --- | --- |
| `--home` | Codex data directory | `CODEX_HOME`, or `.codex` under your user directory if unset |
| `--cli` | Codex CLI executable path | `CODEX_CLEANER_CLI`, or automatic discovery if unset |
| `--port` | Preferred local port | `8765` |
| `--no-browser` | Do not open the browser at startup | The browser opens by default |

You can also change the data directory and CLI path in **Settings** at the top right. Saving detects Codex and loads sessions automatically. These settings apply only to the current run and do not modify Codex's own configuration.

## Manage and clean up sessions

### Browse records

After the initial scan completes, search by title, project, path, or session ID, or filter by file status. Enable **Group by project** to navigate projects from the sidebar.

Sessions and their attached records appear in one list:

- Records with a confirmed parent relationship appear beneath that session. They are collapsed by default and can be expanded when needed.
- Selecting a parent session also selects all its linked descendants, even when collapsed.
- **Unlinked records** cannot currently be associated with an existing parent session. **Unlinked does not mean useless or safe to delete automatically.**
- When a child matches a filter, its parent may appear for context. A parent that does not match the filter cannot be selected from that context row and is not included in Select all.

Relationships come from explicit fields in the Codex database and log headers. Sharing a project or having similar timestamps is not treated as proof of a relationship.

### Preview content

Click a title to view its content in the right pane. Drag the pane boundary to resize it, or use **Clear preview** to dismiss the current content.

Previews focus on user messages and identifiable assistant replies; they are not complete conversation exports. Images appear as placeholders. Very large messages, some older records, and replies without a final-response marker may be omitted.

### Delete sessions

1. Select the records to clean up. The table header checkbox selects the current filtered results; a group checkbox selects matching records within that group.
2. Click **Review deletion** and check the sessions, attached records, and estimated space in the plan.
3. Confirm permanent deletion and wait for the results. You do not need to type a confirmation phrase for each record.

**Deletion is irreversible. The tool provides neither a recycle bin nor automatic backups.** Changing filters does not clear previous selections. Check the selection summary and deletion plan before proceeding.

Deletion uses the official CLI. The tool checks session metadata, history indexes, and the original associated files afterward, rather than treating a successful command exit as proof of complete cleanup. Batch deletion runs sequentially. You can stop subsequent deletions, but completed deletions cannot be undone.

Codex can remain open. Current sessions, sessions in use, targets whose status cannot be verified, and records with conflicting file ownership are protected. The tool does not force Codex to close or release its locks.

### Clean up records with missing files

Choose **All files missing**, then use the table or group checkbox to select matching records. A healthy parent is not selected merely because a child's files are missing.

These records may remain after files were deleted manually. Because cleanup removes remaining session records, a successful result may reclaim **0 B**. Unreadable files and nonexistent files are different conditions and are not handled interchangeably.

## Database maintenance

Open **Database maintenance** at the top of the page to inspect database size and internal free space:

| Database | Main contents |
| --- | --- |
| `thread_history_1.sqlite` | Conversation turns, messages, and history indexes |
| `state_5.sqlite` | Session metadata, projects, and related state |
| `logs_2.sqlite` | Codex runtime diagnostic logs |

### Deleting records versus reclaiming space

Session deletion invokes the official command to remove the corresponding records. However, SQLite usually retains freed pages for future writes, so the database file may not shrink immediately.

Database maintenance compacts those pages to reclaim file space. Run it after cleaning up a batch of sessions; there is no need to compact after every deletion.

### Run maintenance

1. Select the databases to compact.
2. By default, all logs are retained and only unused space is reclaimed. To remove logs, choose to retain the most recent 7, 30, or 90 days, or remove all logs older than the plan's cutoff time.
3. Review the maintenance plan and confirm execution.
4. Check each database's result and the actual space reclaimed.

Maintenance preserves existing sessions and history. It does not delete a history record simply because matching session metadata is absent, and it does not delete entire database files.

Compaction requires temporary disk space. The tool reports database contention, insufficient space, or failed checks. Maintenance can be stopped, but committed log deletions are not rolled back. Maintenance, session deletion, and scanning cannot run concurrently.

## FAQ

### Why can the deletion plan show a different size from the list?

A session row includes the file sizes of children known to the official session relationship data. Additional attached relationships recovered from log headers may not belong to the same official deletion scope. Selecting a parent also selects those attached records for the plan, so use the deletion plan as the final scope of the operation.

Session storage figures represent the logical sizes of JSONL files with confirmed ownership. They exclude shared databases, image attachments, and filesystem compression differences. Database maintenance measures database and WAL files separately. Concurrent Codex writes can also affect changes in actual free disk space.

### What are Scan issues?

These are findings such as missing files, read failures, unexpected paths, or uncertain ownership. Click **Scan issues** in the status bar to inspect them. Use **Recheck files** in that window when you need another scan. The issue count is neither a count of files ready for deletion nor an estimate of reclaimable space.

### Why are some records blocked from deletion?

A session may be in use by Codex, the CLI version may be incompatible, permissions may be insufficient, or file ownership may be uncertain. The deletion plan or execution result explains the specific reason. The tool does not bypass these checks to delete files directly.

### Does changing the language change my conversations?

No. Only the interface changes. Session titles, messages, project names, and paths remain in their original language. On first use, a Chinese browser language selects Chinese; other languages select English. A saved manual choice takes precedence.

## Development

The backend uses the Python standard library. The frontend uses vanilla JavaScript, HTML, and CSS.

```text
app.py          Local HTTP service and startup entry point
cleaner/        Session scanning, official CLI cleanup, and database maintenance
web/            Pages, styles, and frontend logic
web/locales/    Chinese and English language resources
tests/          Automated tests and development verification scripts
```

Run the automated tests:

```powershell
python -m unittest discover -s tests -v
```

Browser verification scripts use Playwright. Node.js and Playwright are needed only for development checks. Read each script's service address, mocked endpoints, and environment-variable configuration before running it. The isolated official CLI verification script, `tests/probe_official_delete.py`, creates and deletes temporary test sessions.

When adding interface text, update both `web/locales/zh-CN.json` and `web/locales/en.json`. Do not commit local Codex data, conversation content, or debug screenshots containing private information.
