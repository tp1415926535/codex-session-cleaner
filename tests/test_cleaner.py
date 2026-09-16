import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

from app import make_server
from cleaner.core import Store, PAGE_BYTES, canonical, classify, detect_cli, user_text, run_cli
from cleaner.activity import VERIFIED_VERSION, thread_activity, probe_file


@contextmanager
def fixture_db(path):
    c = sqlite3.connect(path)
    try:
        with c:
            yield c
    finally:
        c.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cleaner 中文 space ')
        self.home = Path(self.tmp.name)
        (self.home / 'sessions').mkdir()
        self.db = self.home / 'state_5.sqlite'
        with fixture_db(self.db) as c:
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,source TEXT,cwd TEXT,updated_at INTEGER,archived INTEGER,name TEXT,first_user_message TEXT,thread_source TEXT,history_mode TEXT)')
        self.store = Store(self.home, guard=lambda: '')
        self.store.current = set()

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, sid, name=None, extra=None):
        p = self.home / 'sessions' / (name or f'rollout-{sid}.jsonl')
        p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': sid, **(extra or {})}}) + '\n', encoding='utf-8')
        return p

    def row(self, sid=None, path=None, source='vscode', title='测试会话', name=None, first='', kind='user', history='legacy'):
        sid = sid or str(uuid.uuid4())
        path = path or self.file(sid)
        with fixture_db(self.db) as c:
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)', (sid, str(path), title, source, 'D:\\中文 project', 1, 0, name, first, kind, history))
        return sid, path

    def scan(self):
        with patch('cleaner.core.detect_cli', return_value={'available': True, 'path': 'mock codex.exe', 'version': 'test'}):
            self.store.scan()
        self.assertTrue(self.store.complete, self.store.error)

    def test_internal_preview_exposes_review_content(self):
        sid, path = self.row(source='{"subagent":{"other":"guardian"}}', kind='guardian_review', title='')
        verdict = '{"risk_level":"low","user_authorization":"explicit","outcome":"allow","rationale":"read only"}'
        with path.open('a', encoding='utf-8') as f:
            for role, text in [('user', 'Review command: git status'), ('assistant', verdict)]:
                f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}]}}) + '\n')
        self.scan()
        row = self.store.rows[sid]
        self.assertEqual(row['record_type'], '操作安全审批')
        self.assertTrue(row['title'].startswith('操作安全审批'))
        preview = self.store.preview(sid)
        self.assertEqual(len(preview['messages']), 2)
        self.assertIn('git status', preview['messages'][0]['text'])
        self.assertEqual(preview['messages'][1]['text'], verdict)

    def test_large_batch_and_descendants_have_no_count_cap(self):
        sid, _ = self.row()
        self.scan()
        template = self.store.rows[sid]
        ids = [sid] + [str(uuid.uuid4()) for _ in range(220)]
        for child in ids[1:]:
            self.store.rows[child] = {**template, 'id': child, 'files': [], 'missing_files': []}
        self.assertEqual(len(self.store.plan(ids)['entries']), 221)
        self.store.edges[sid].update(ids[1:])
        plan = self.store.plan([sid])
        self.assertEqual(len(plan['entries']), 221)
        self.assertTrue(plan['allowed'])
        self.assertEqual(self.store.plan(ids)['roots'], [sid])
        with self.assertRaises(ValueError): self.store.plan([])
        with self.assertRaises(ValueError): self.store.plan([str(uuid.uuid4())])

    def test_dangling_child_relation_does_not_block_plan(self):
        parent, _ = self.row()
        grandchild, _ = self.row()
        missing = str(uuid.uuid4())
        self.scan()
        self.store.edges[parent].add(missing)
        self.store.edges[missing].add(grandchild)
        plan = self.store.plan([parent])
        self.assertTrue(plan['allowed'])
        self.assertEqual(plan['missing_descendants'], [missing])
        self.assertEqual({e['id'] for e in plan['entries']}, {parent, grandchild})
        self.store.current.add(missing)
        self.assertFalse(self.store.plan([parent])['allowed'])
        self.store.current.clear()
        self.store.diagnostics.append({'owner': missing})
        self.assertFalse(self.store.plan([parent])['allowed'])

    def test_delete_recheck_preserves_verified_cli_version(self):
        parent, _ = self.row()
        missing = str(uuid.uuid4())
        with fixture_db(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (parent, missing))
        self.scan()
        self.store.guard = None
        self.store.cli['version'] = VERIFIED_VERSION
        def activity(home, sid, version):
            self.assertEqual(version, VERIFIED_VERSION)
            return {'code': 'idle', 'label': '未占用', 'reason': ''}
        self.store.runner = lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '', 'fixture failure')
        with patch('cleaner.core.thread_activity', side_effect=activity):
            self.store.delete(self.store.plan([parent])['token'])

    def test_environment_failure_stops_remaining_commands(self):
        a, _ = self.row()
        b, _ = self.row()
        self.scan()
        calls = []
        def failed(args, home, timeout):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, '', 'Error: failed to initialize state database')
        self.store.runner = failed
        result = self.store.delete(self.store.plan([a,b])['token'])
        self.assertEqual(len(calls), 1)
        self.assertEqual([r['status'] for r in result['results']], ['失败', '未执行'])

    def test_stop_finishes_current_then_skips_remaining(self):
        a, _ = self.row()
        b, _ = self.row()
        self.scan()
        calls = []
        delete = self.mock_delete('success')
        def runner(args, home, timeout):
            calls.append(args)
            self.store.stop_delete()
            return delete(args, home, timeout)
        self.store.runner = runner
        result = self.store.delete(self.store.plan([a,b])['token'])
        self.assertEqual(len(calls), 1)
        self.assertEqual([r['status'] for r in result['results']], ['成功', '未执行'])
        self.assertFalse(self.store.delete_progress['running'])

    def test_paginated_multi_rollout_reads_beyond_projection(self):
        sid, first = self.row(history='paginated')
        second = self.file(sid, name='second.jsonl')
        for path, text in [(first, '开头回复'), (second, '后续回复')]:
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': text}]}}) + '\n')
        with fixture_db(self.home / 'thread_history_1.sqlite') as c:
            c.execute('CREATE TABLE placeholder(id)')
        self.scan()
        page = self.store.preview(sid)
        self.assertEqual(page['messages'][0]['text'], '开头回复')
        self.assertIsNotNone(page['next'])
        page = self.store.preview(sid, page['next'], page['file_index'])
        self.assertEqual(page['messages'][0]['text'], '后续回复')
        self.assertIsNone(page['next'])

    def test_classification_titles(self):
        a, _ = self.row(title='# AGENTS.md instructions\n<INSTRUCTIONS>noise', first='真正的问题')
        b, _ = self.row(source='{"subagent":{"other":"guardian"}}', kind='guardian_review')
        c, _ = self.row(title='如何编辑 AGENTS.md', name='用户重命名')
        d, _ = self.row(source='unrecognized', kind='unknown')
        self.scan()
        self.assertEqual(self.store.rows[a]['title'], '真正的问题')
        self.assertEqual(self.store.rows[b]['kind'], 'internal')
        self.assertEqual(self.store.rows[c]['title'], '用户重命名')
        self.assertEqual(self.store.rows[d]['kind'], 'unknown')
        self.assertFalse(self.store.plan([d])['allowed'])

    def test_multiple_uuid_and_parent_not_owner(self):
        a, p = self.row()
        b, _ = self.row()
        second = self.file(a, f'rollout-{b}-{a}.jsonl', {'session_id': b, 'parent_thread_id': b})
        self.scan()
        self.assertEqual(len(self.store.rows[a]['files']), 2)
        self.assertEqual(self.store.rows[a]['size'], p.stat().st_size + second.stat().st_size)
        self.assertEqual(len(self.store.rows[b]['files']), 1)

    def test_shared_unknown_outside(self):
        a, p = self.row()
        b, _ = self.row(path=p)
        orphan = self.file(str(uuid.uuid4()))
        outside = self.home / 'outside.jsonl'
        outside.write_text('{}')
        c, _ = self.row(path=outside)
        self.scan()
        self.assertEqual(self.store.rows[a]['size'], 0)
        self.assertEqual(self.store.rows[b]['size'], 0)
        self.assertFalse(self.store.plan([a])['allowed'])
        self.assertFalse(self.store.plan([c])['allowed'])
        self.assertTrue(any(d['path'] == str(orphan) for d in self.store.diagnostics))

    def test_current_and_guard(self):
        a, _ = self.row()
        self.scan()
        self.store.current = {a}
        self.assertFalse(self.store.plan([a])['allowed'])
        self.store.current = set()
        self.store.guard = lambda: '活动未知'
        self.assertFalse(self.store.plan([a])['allowed'])

    def test_bounded_legacy_and_xss_literal(self):
        a, p = self.row()
        def message(role, text, phase=None):
            return json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': role, 'phase': phase, 'content': [{'type': 'text', 'text': text}]}}) + '\n'
        with p.open('a', encoding='utf-8') as f:
            f.write(message('user', '<img src=x onerror=alert(1)>'))
            f.write(message('assistant', '进度', 'commentary'))
            f.write(message('assistant', '未知阶段'))
            f.write(message('assistant', '最终', 'final'))
            f.write('x' * (PAGE_BYTES * 2) + '\n')
            f.write(message('user', '结尾'))
        self.scan()
        one = self.store.preview(a)
        self.assertEqual([x['text'] for x in one['messages']], ['<img src=x onerror=alert(1)>', '最终'])
        self.assertLessEqual(one['next'], PAGE_BYTES)
        two = self.store.preview(a, one['next'])
        three = self.store.preview(a, two['next'])
        self.assertEqual(three['messages'][0]['text'], '结尾')

    def test_paginated_final_and_large_images(self):
        a, _ = self.row(history='paginated')
        with fixture_db(self.home / 'thread_history_1.sqlite') as c:
            c.execute('CREATE TABLE thread_items(thread_id,turn_id,item_id,rollout_ordinal,item_type,item_json)')
            c.execute('CREATE TABLE thread_turns(thread_id,turn_id,final_agent_item_id)')
            c.execute('INSERT INTO thread_turns VALUES(?,?,?)', (a, 'turn', 'final'))
            items = [('u','userMessage',{'content':[{'type':'text','text':'用户问题'},{'type':'image','url':'data:image/png;base64,aaaa'}]}),('c','agentMessage',{'phase':'commentary','text':'进度'}),('final','agentMessage',{'text':'最终'}),('big','userMessage',{'content':[{'type':'image','url':'x'*100000}]})]
            for i, (key, typ, item) in enumerate(items):
                c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)', (a, 'turn', key, i, typ, json.dumps(item)))
        self.scan()
        preview = self.store.preview(a)
        text = json.dumps(preview, ensure_ascii=False)
        self.assertIn('最终', text)
        self.assertNotIn('进度', text)
        self.assertNotIn('base64', text)
        self.assertIn('超大消息', text)

    def mock_delete(self, mode):
        def runner(args, home, timeout):
            self.assertEqual(home, self.home)
            self.assertEqual(args[1:3], ['delete', '--force'])
            sid = args[3]
            if mode != 'fail':
                with fixture_db(self.db) as c: c.execute('DELETE FROM threads WHERE id=?', (sid,))
            if mode == 'success':
                for f in self.store.rows[sid]['files']: Path(f['path']).unlink()
            return subprocess.CompletedProcess(args, 1 if mode == 'fail' else 0, '', '模拟 CLI')
        return runner

    def test_delete_success_and_replay(self):
        a, _ = self.row()
        self.scan()
        self.store.runner = self.mock_delete('success')
        plan = self.store.plan([a])
        result = self.store.delete(plan['token'])['results'][0]
        self.assertEqual(result['status'], '成功')
        self.assertGreater(result['released'], 0)
        with self.assertRaises(ValueError): self.store.delete(plan['token'])

    def test_partial_and_failure(self):
        for mode, expected in [('partial', '部分完成'), ('fail', '失败')]:
            a, p = self.row()
            self.scan()
            self.store.runner = self.mock_delete(mode)
            result = self.store.delete(self.store.plan([a])['token'])['results'][0]
            self.assertEqual(result['status'], expected)
            self.assertTrue(p.exists())
            self.assertTrue(result['leftovers'])

    def test_changed_file_blocked(self):
        a, p = self.row()
        self.scan()
        plan = self.store.plan([a])
        p.write_text('changed')
        with self.assertRaises(ValueError): self.store.delete(plan['token'])

    def test_missing_rollout_can_clean_metadata_with_zero_release(self):
        a, p = self.row()
        p.unlink()
        self.scan()
        plan = self.store.plan([a])
        self.assertTrue(plan['allowed'])
        self.assertEqual(plan['bytes'], 0)
        self.assertEqual(plan['entries'][0]['missing_files'], [str(p)])
        self.assertTrue(plan['entries'][0]['warnings'])
        self.store.runner = self.mock_delete('success')
        result = self.store.delete(plan['token'])['results'][0]
        self.assertEqual(result['status'], '成功')
        self.assertEqual(result['released'], 0)
        self.assertFalse(result['metadata_present'])

    def test_reappearing_missing_file_invalidates_plan(self):
        a, p = self.row()
        p.unlink()
        self.scan()
        plan = self.store.plan([a])
        self.file(a)
        with self.assertRaises(ValueError): self.store.delete(plan['token'])

    def test_permission_error_is_not_missing(self):
        a, _ = self.row()
        with patch.object(self.store, 'header', side_effect=PermissionError('fixture access denied')):
            self.scan()
        self.assertFalse(self.store.plan([a])['allowed'])
        self.assertEqual(self.store.rows[a]['missing_files'], [])

    def test_new_related_file_invalidates_plan(self):
        a, _ = self.row()
        self.scan()
        plan = self.store.plan([a])
        self.file(a, 'another-rollout.jsonl')
        with self.assertRaises(ValueError): self.store.delete(plan['token'])

    def test_serial_delete_and_expiration(self):
        a, _ = self.row()
        self.scan()
        plan = self.store.plan([a])
        self.store.operation.acquire()
        try:
            with self.assertRaises(ValueError): self.store.delete(plan['token'])
        finally: self.store.operation.release()
        plan['expires'] = 0
        with self.assertRaises(ValueError): self.store.delete(plan['token'])

    def test_history_residue_is_partial(self):
        a, _ = self.row()
        with fixture_db(self.home / 'thread_history_1.sqlite') as c:
            c.execute('CREATE TABLE thread_items(thread_id)')
            c.execute('INSERT INTO thread_items VALUES(?)', (a,))
        self.scan()
        self.store.runner = self.mock_delete('success')
        result = self.store.delete(self.store.plan([a])['token'])['results'][0]
        self.assertEqual(result['status'], '部分完成')
        self.assertTrue(result['history_remaining']['thread_items'])

    def test_cli_timeout_is_not_success(self):
        a, _ = self.row()
        self.scan()
        def timeout(*args, **kwargs): raise subprocess.TimeoutExpired('fake', 120)
        self.store.runner = timeout
        result = self.store.delete(self.store.plan([a])['token'])['results'][0]
        self.assertEqual(result['status'], '失败')
        self.assertIsNone(result['exit_code'])

    def test_cli_home_and_no_shell(self):
        with patch('cleaner.core.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as mocked:
            run_cli(['C:\\中文 space\\codex.exe', 'delete', '--force', str(uuid.uuid4())], self.home)
        self.assertEqual(mocked.call_args.kwargs['env']['CODEX_HOME'], str(self.home))
        self.assertIs(mocked.call_args.kwargs['shell'], False)

    def test_oversized_header_and_cancel(self):
        a, p = self.row()
        p.write_text('x' * 500000 + '\n')
        self.scan()
        self.assertEqual(self.store.rows[a]['size'], p.stat().st_size)
        self.store.cancel.set()
        self.store.complete = False
        with patch('cleaner.core.detect_cli', return_value=self.store.cli): self.store.scan()
        self.assertFalse(self.store.complete)

    def test_attachment_wrapper_removed(self):
        self.assertEqual(user_text('# Files mentioned by the user:\nnoise\n## My request:\n真实请求'), '真实请求')

    def test_hardlinks_blocked(self):
        a, p = self.row()
        alias = p.with_name('alias.jsonl')
        try: os.link(p, alias)
        except OSError: self.skipTest('hardlinks unavailable')
        self.scan()
        self.assertEqual(self.store.rows[a]['size'], 0)
        self.assertFalse(self.store.plan([a])['allowed'])

    @unittest.skipUnless(os.name == 'nt', 'Windows lock protocol')
    def test_online_idle_and_busy_are_independent(self):
        import msvcrt
        a, _ = self.row()
        b, _ = self.row()
        self.scan()
        self.store.guard = None
        self.store.cli['version'] = VERIFIED_VERSION
        locks = self.home / 'thread-writer-locks'
        locks.mkdir()
        (locks / '.coordination.lock').touch()
        lock = locks / f'{a}.lock'
        lock.touch()
        with lock.open('r+b') as held:
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            self.assertEqual(probe_file(lock), 'busy')
            self.assertFalse(self.store.plan([a])['allowed'])
            self.assertTrue(self.store.plan([b])['allowed'])
            held.seek(0); msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        self.assertTrue(self.store.plan([a])['allowed'])
        self.assertEqual(lock.stat().st_size, 0)

    def test_unverified_protocol_blocks_target(self):
        a, _ = self.row()
        self.scan()
        self.store.guard = None
        self.store.cli['version'] = 'unknown-version'
        self.assertFalse(self.store.plan([a])['allowed'])

    def test_target_becomes_busy_after_confirmation(self):
        a, _ = self.row()
        self.scan()
        plan = self.store.plan([a])
        self.store.guard = lambda: '目标已占用'
        with self.assertRaisesRegex(ValueError, '目标已占用'):
            self.store.delete(plan['token'])

    def test_descendants_in_plan_and_current_child_protected(self):
        a, _ = self.row()
        b, _ = self.row(source='{"subagent":{}}', kind='subagent')
        with fixture_db(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (a,b))
        self.scan()
        plan = self.store.plan([a,b])
        self.assertEqual(plan['roots'], [a])
        self.assertEqual({e['id'] for e in plan['entries']}, {a,b})
        self.assertTrue(plan['entries'][1]['included_descendant'])
        self.store.current = {b}
        self.assertFalse(self.store.plan([a])['allowed'])

    def test_new_descendant_invalidates_confirmation(self):
        a, _ = self.row()
        b, _ = self.row(source='{"subagent":{}}', kind='subagent')
        self.scan()
        plan = self.store.plan([a])
        with fixture_db(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (a,b))
        with self.assertRaisesRegex(ValueError, '子会话关系已变化'):
            self.store.delete(plan['token'])

    def test_cascade_verifies_each_child_with_one_cli_call(self):
        a, _ = self.row()
        b, _ = self.row(source='{"subagent":{}}', kind='subagent')
        with fixture_db(self.db) as c:
            c.execute('CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id)')
            c.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (a,b))
        self.scan()
        calls = []
        def cascade(args, home, timeout):
            self.assertTrue(self.store.delete_progress['running'])
            self.assertEqual(self.store.delete_progress['total'], 2)
            calls.append(args)
            for sid in (a,b):
                for f in self.store.rows[sid]['files']: Path(f['path']).unlink()
            with fixture_db(self.db) as c:
                c.execute('DELETE FROM threads')
                c.execute('DELETE FROM thread_spawn_edges')
            return subprocess.CompletedProcess(args,0,'','')
        self.store.runner = cascade
        result = self.store.delete(self.store.plan([a,b])['token'])
        self.assertEqual(self.store.delete_progress['done'], 2)
        self.assertFalse(self.store.delete_progress['running'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][-1],a)
        self.assertEqual({r['id'] for r in result['results']},{a,b})
        self.assertTrue(all(r['status']=='成功' for r in result['results']))

    def test_missing_cli(self):
        result = detect_cli(self.home, str(self.home / '不存在 codex.exe'))
        self.assertFalse(result['available'])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path')
    def test_extended_paths(self):
        a, p = self.row()
        with fixture_db(self.db) as c: c.execute('UPDATE threads SET rollout_path=? WHERE id=?', ('\\\\?\\' + str(p), a))
        self.scan()
        self.assertEqual(len(self.store.rows[a]['files']), 1)
        self.assertFalse(self.store.rows[a]['issues'])
        self.assertEqual(canonical('\\\\?\\' + str(p)), p)

    def test_http_security_and_port_fallback(self):
        a, _ = self.row()
        self.scan()
        server = make_server(self.store, 0)
        other = make_server(self.store, server.server_port)
        self.assertNotEqual(server.server_port, other.server_port)
        other.server_close()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            data = json.load(urllib.request.urlopen(base + '/api/state'))
            for headers in ({}, {'Origin': 'https://evil.example', 'X-CSRF-Token': data['csrf']}, {'Origin': base, 'X-CSRF-Token': data['csrf'], 'Host': 'evil.example'}):
                req = urllib.request.Request(base + '/api/plan', data=b'{}', headers={'Content-Type':'application/json', **headers})
                with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req)
                self.assertEqual(ctx.exception.code, 403)
            req = urllib.request.Request(base + '/api/plan', data=json.dumps({'ids':[a]}).encode(), headers={'Content-Type':'application/json','Origin':base,'X-CSRF-Token':data['csrf']})
            self.assertIn('token', json.load(urllib.request.urlopen(req)))
            with urllib.request.urlopen(base) as r:
                self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__ == '__main__': unittest.main()


class LocaleCatalogTests(unittest.TestCase):
    def test_catalog_keys_placeholders_and_html_bindings(self):
        import re
        web = Path(__file__).resolve().parents[1] / 'web'
        zh = json.loads((web / 'locales' / 'zh-CN.json').read_text(encoding='utf-8'))
        en = json.loads((web / 'locales' / 'en.json').read_text(encoding='utf-8'))
        self.assertEqual(set(zh), set(en))
        for key in zh:
            self.assertEqual(set(re.findall(r'\{(\w+)\}', zh[key])), set(re.findall(r'\{(\w+)\}', en[key])), key)
        markup = (web / 'index.html').read_text(encoding='utf-8')
        for key in re.findall(r'data-i18n(?:-[\w-]+)?="([^"]+)"', markup):
            self.assertIn(key, zh)
        for key in re.findall(r"msg\('([^']+)'", (web / 'app.js').read_text(encoding='utf-8')):
            self.assertIn(key, zh)
