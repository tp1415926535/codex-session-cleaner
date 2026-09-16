"""Opt-in installed-CLI contract test. Deletes ONLY its own TemporaryDirectory.
Run: python tests/probe_official_delete.py
No real CODEX_HOME is ever passed to a mutating command.
"""
import json
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import sys
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleaner.activity import probe_file


def main():
    cli = shutil.which('codex')
    assert cli, 'CLI missing'
    with tempfile.TemporaryDirectory(prefix='cleaner-online-contract-') as td:
        home = Path(td).resolve()
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home)
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
            env.pop(name, None)
        sid = str(uuid.uuid4())
        path = home / 'sessions' / f'rollout-2026-09-16T00-00-00-{sid}.jsonl'
        path.parent.mkdir()
        path.write_text(json.dumps({'timestamp':'2026-09-16T00:00:00Z','type':'session_meta','payload':{
            'id':sid,'timestamp':'2026-09-16T00:00:00Z','cwd':str(home),'originator':'codex_cli_rs',
            'cli_version':'0.154.0-alpha.6.2','source':'cli','model_provider':'openai'}})+'\n', encoding='utf-8')
        locks = home / 'thread-writer-locks'
        locks.mkdir()
        lock = locks / f'{sid}.lock'
        lock.write_bytes(b'')
        with lock.open('r+b') as held:
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            assert probe_file(lock) == 'busy'
            blocked = subprocess.run([cli,'delete','--force',sid],env=env,capture_output=True,encoding='utf-8',errors='replace',timeout=45)
            assert blocked.returncode != 0 and path.exists(), (blocked.stdout, blocked.stderr)
            held.seek(0); msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        assert probe_file(lock) == 'free'
        deleted = subprocess.run([cli,'delete','--force',sid],env=env,capture_output=True,encoding='utf-8',errors='replace',timeout=45)
        assert deleted.returncode == 0 and not path.exists(), (deleted.stdout,deleted.stderr)
        # Seed an orphan in the isolated CLI-created schema; no real DB is copied.
        orphan = str(uuid.uuid4())
        missing = home / 'sessions' / f'rollout-2026-09-16T00-00-00-{orphan}.jsonl'
        db = home / 'state_5.sqlite'
        assert db.exists()
        conn = sqlite3.connect(db)
        try:
            conn.execute('''INSERT INTO threads(id,rollout_path,created_at,updated_at,source,
                model_provider,cwd,title,sandbox_policy,approval_mode)
                VALUES(?,?,0,0,'cli','openai',?,'orphan fixture','read-only','never')''',
                (orphan,str(missing),str(home)))
            conn.execute('INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,?)', (orphan,str(uuid.uuid4()),'completed'))
            conn.commit()
        finally: conn.close()
        cleaned = subprocess.run([cli,'delete','--force',orphan],env=env,capture_output=True,encoding='utf-8',errors='replace',timeout=45)
        conn = sqlite3.connect(db)
        try: remaining = conn.execute('SELECT count(*) FROM threads WHERE id=?',(orphan,)).fetchone()[0]
        finally: conn.close()
        assert cleaned.returncode == 0 and remaining == 0 and not missing.exists(), (cleaned.stdout,cleaned.stderr,remaining)
        print(json.dumps({'locked_delete':'refused, rollout intact','unlocked_delete':'success, rollout removed',
                          'missing_rollout':'orphan metadata removed successfully',
                          'read_only_probe':'busy/free matches CLI','home':'isolated temporary directory'},ensure_ascii=False))


if __name__ == '__main__': main()
