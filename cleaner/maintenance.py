"""Shared SQLite maintenance. Never infers deletable history from missing metadata."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

DATABASES = ('thread_history_1.sqlite', 'logs_2.sqlite', 'state_5.sqlite')
HISTORY_TABLES = ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items')


class DatabaseMaintenance:
    def __init__(self, store):
        self.store = store
        self.plans = {}
        self.stop = threading.Event()
        self.progress = {'running': False, 'done': 0, 'total': 0, 'results': []}

    def path(self, name):
        if name not in DATABASES:
            raise ValueError('Unsupported database')
        home = self.store.home.resolve()
        path = home / name
        if path.is_symlink() or path.resolve().parent != home:
            raise ValueError('Database path is outside the data directory')
        return path

    @staticmethod
    @contextlib.contextmanager
    def connection(path, writable=False):
        conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=1)
        try:
            if not writable:
                conn.execute('PRAGMA query_only=ON')
                deadline = time.monotonic() + 15
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def disk_bytes(path):
        return sum(p.stat().st_size for p in (path, Path(str(path) + '-wal')) if p.exists())

    def inspect(self):
        reports = []
        for name in DATABASES:
            path = self.path(name)
            if not path.exists():
                reports.append({'name': name, 'exists': False})
                continue
            try:
                with self.connection(path) as c:
                    pages = c.execute('PRAGMA page_count').fetchone()[0]
                    page_size = c.execute('PRAGMA page_size').fetchone()[0]
                    free = c.execute('PRAGMA freelist_count').fetchone()[0]
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    report = {'name': name, 'exists': True, 'bytes': path.stat().st_size,
                              'wal_bytes': max(0, self.disk_bytes(path) - path.stat().st_size),
                              'logical_bytes': pages * page_size, 'free_bytes': free * page_size}
                    if name == 'logs_2.sqlite' and 'logs' in tables:
                        report['records'] = c.execute('SELECT count(*) FROM logs').fetchone()[0]
                    if name == 'thread_history_1.sqlite':
                        state = self.path('state_5.sqlite')
                        if state.exists():
                            c.execute('ATTACH DATABASE ? AS session_state', (state.as_uri() + '?mode=ro',))
                            missing = set()
                            counts = {}
                            for table in HISTORY_TABLES:
                                if table not in tables: continue
                                rows = c.execute(f'''SELECT thread_id,count(*) FROM {table} h
                                    WHERE NOT EXISTS(SELECT 1 FROM session_state.threads s WHERE s.id=h.thread_id)
                                    GROUP BY thread_id''').fetchall()
                                missing.update(sid for sid, _ in rows)
                                counts[table] = sum(n for _, n in rows)
                            report.update(unmatched_sessions=len(missing), unmatched_records=counts)
                    reports.append(report)
            except (OSError, sqlite3.Error) as e:
                reports.append({'name': name, 'exists': True, 'error': str(e)})
        return {'databases': reports}

    def plan(self, names, log_days=None):
        if self.store.operation.locked() or (self.store.worker and self.store.worker.is_alive()):
            raise ValueError('Another cleanup or scan is running')
        if not isinstance(names, list) or not names or any(name not in DATABASES for name in names):
            raise ValueError('Select at least one supported database')
        names = list(dict.fromkeys(names))
        if log_days is not None and (type(log_days) is not int or log_days not in (0, 7, 30, 90)):
            raise ValueError('Invalid log retention period')
        if log_days is not None and 'logs_2.sqlite' not in names:
            raise ValueError('Select the logs database to clean logs')
        cutoff = int(time.time()) - log_days * 86400 if log_days is not None else None
        reports = {r['name']: r for r in self.inspect()['databases']}
        entries = []
        for name in names:
            report = reports[name]
            if not report['exists'] or report.get('error'):
                raise ValueError(name + ': ' + report.get('error', 'Database not found'))
            entry = {**report, 'logs_to_delete': 0}
            if name == 'logs_2.sqlite' and cutoff is not None:
                with self.connection(self.path(name)) as c:
                    entry['logs_to_delete'] = c.execute('SELECT count(*) FROM logs WHERE ts < ?', (cutoff,)).fetchone()[0]
            entries.append(entry)
        token = uuid.uuid4().hex
        plan = {'token': token, 'home': str(self.store.home.resolve()), 'expires': time.time() + 300,
                'entries': entries, 'log_days': log_days, 'cutoff': cutoff}
        self.plans[token] = plan
        return plan

    def start(self, token):
        if not self.store.operation.acquire(False):
            raise ValueError('Another cleanup or scan is running')
        try:
            if self.store.worker and self.store.worker.is_alive():
                raise ValueError('Another cleanup or scan is running')
            plan = self.plans.pop(token, None)
            if not plan or plan['expires'] < time.time() or plan['home'] != str(self.store.home.resolve()):
                raise ValueError('Maintenance plan expired; check again')
            self.stop.clear()
            self.progress = {'running': True, 'done': 0, 'total': len(plan['entries']), 'phase': 'checking',
                             'database': '', 'started': time.time(), 'results': []}
            worker = threading.Thread(target=self._run, args=(plan,), daemon=True)
            worker.start()
            return {'ok': True}
        except Exception:
            self.store.operation.release()
            raise

    def cancel(self):
        if self.progress['running']:
            self.stop.set()
            self.progress = {**self.progress, 'stopping': True}
        return {'ok': True}

    def _run(self, plan):
        results = []
        try:
            for entry in plan['entries']:
                name = entry['name']
                if self.stop.is_set():
                    results.append({'name': name, 'status': 'skipped', 'deleted_logs': 0, 'released': 0})
                    continue
                self.progress = {**self.progress, 'database': name, 'phase': 'checking'}
                result = {'name': name, 'status': 'failed', 'deleted_logs': 0, 'released': 0}
                path = None
                before = 0
                try:
                    path = self.path(name)
                    before = self.disk_bytes(path)
                    # VACUUM is transactional and may require twice the DB size in spare disk.
                    if shutil.disk_usage(path.parent).free < max(path.stat().st_size * 2, 16 * 1024 * 1024):
                        raise ValueError('Not enough temporary disk space to compact the database')
                    with self.connection(path, writable=True) as c:
                        c.set_progress_handler(lambda: int(self.stop.is_set()), 10000)
                        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise ValueError('Database integrity check failed; no cleanup performed')
                        if name == 'logs_2.sqlite' and plan['cutoff'] is not None:
                            self.progress = {**self.progress, 'phase': 'logs'}
                            c.execute('BEGIN IMMEDIATE')
                            try:
                                deleted = c.execute('DELETE FROM logs WHERE ts < ?', (plan['cutoff'],)).rowcount
                                if self.stop.is_set(): raise InterruptedError('Stopped')
                                c.commit()
                                result['deleted_logs'] = deleted
                            except Exception:
                                c.rollback()
                                raise
                        if self.stop.is_set(): raise InterruptedError('Stopped')
                        self.progress = {**self.progress, 'phase': 'compacting'}
                        c.execute('VACUUM')
                        self.progress = {**self.progress, 'phase': 'verifying'}
                        checkpoint = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise ValueError('Database integrity check failed after compaction')
                        result['status'] = 'partial' if checkpoint[0] else 'success'
                        if checkpoint[0]: result['error'] = 'Database is in use; WAL space will be reclaimed later'
                except Exception as e:
                    result['status'] = 'stopped' if self.stop.is_set() else ('partial' if result['deleted_logs'] else 'failed')
                    result['error'] = str(e)
                if path is not None:
                    try: result['released'] = max(0, before - self.disk_bytes(path))
                    except OSError: pass
                results.append(result)
                self.progress = {**self.progress, 'done': len(results), 'results': list(results)}
        finally:
            self.progress = {**self.progress, 'running': False, 'done': len(results), 'results': results}
            self.store.operation.release()
