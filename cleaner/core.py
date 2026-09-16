from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import threading
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from .activity import thread_activity

Path = pathlib.Path
HEADER_LIMIT = 256 * 1024
RECORD_LIMIT = 256 * 1024
PAGE_BYTES = 4 * 1024 * 1024
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def canonical(path):
    value = str(path)
    if os.name == 'nt' and value.startswith('\\\\?\\'):
        value = ('\\\\' + value[8:]) if value.startswith('\\\\?\\UNC\\') else value[4:]
    return Path(value).resolve()


def uid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@contextmanager
def db_open(path):
    # mode=ro preserves WAL visibility; immutable=1 would silently miss live rows.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    deadline = time.monotonic() + 8
    try:
        yield conn
    finally:
        conn.close()


def clean_text(text):
    text = str(text or '')
    text = re.sub(r'data:image/[^\s"<>]+', '[图片已隐藏]', text)
    return text[:24000]


def injected(text):
    s = str(text or '').strip()
    return s.startswith(('# AGENTS.md instructions', '<environment_context>', '<permissions',
                         '<INSTRUCTIONS>', '<system', '<developer', '<app-context>',
                         '<recommended_plugins>', '<skills_instructions>'))


def user_text(text):
    text = clean_text(text)
    if text.lstrip().startswith(('# Files mentioned by the user:', '# Files pasted by the user:')):
        if '## My request:' in text:
            text = text.rsplit('## My request:', 1)[1].strip()
            return text or '[附件消息：未展开文件内容]'
        return '[附件消息：未展开文件内容]'
    # A user turn may contain the actual request after injected instructions.
    if injected(text):
        for marker in ('## My request:', '</environment_context>'):
            if marker in text:
                tail = text.rsplit(marker, 1)[1].strip()
                if tail and not injected(tail):
                    return tail
        return ''
    return text


def blocks_text(blocks):
    if isinstance(blocks, str):
        return clean_text(blocks)
    parts = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get('type') in ('text', 'input_text', 'output_text'):
            parts.append(clean_text(b.get('text', '')))
        elif 'image' in b.get('type', '').lower():
            parts.append('[图片占位：未加载原图]')
    return '\n'.join(parts)[:24000]


def classify(row, children=()):
    source = str(row.get('source', ''))
    kind = row.get('thread_source')
    if 'subagent' in source.lower() or kind in ('guardian_review', 'subagent') or row['id'] in children:
        return 'internal'
    if row.get('agent_path') not in (None, '', '/root'):
        return 'internal'
    if kind == 'user' or (kind is None and source in ('vscode', 'cli', 'exec', 'appServer')):
        return 'main'
    return 'unknown'


def run_cli(args, home, timeout=12):
    env = os.environ.copy()
    env['CODEX_HOME'] = str(home)
    return subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          shell=False, creationflags=NO_WINDOW)


def detect_cli(home, explicit=''):
    candidates = [explicit] if explicit else []
    if not explicit:
        candidates.append(shutil.which('codex'))
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI/Codex/bin'
        if base.is_dir():
            candidates.extend(str(p) for p in sorted(base.glob('*/codex.exe'),
                                                    key=lambda p: p.stat().st_mtime, reverse=True))
    errors = []
    for candidate in dict.fromkeys(candidates):
        if not candidate:
            continue
        p = Path(candidate).expanduser().resolve()
        if p.suffix.lower() in ('.bat', '.cmd'):
            errors.append('不执行 shell 包装脚本，请指定 codex.exe'); continue
        try:
            v = run_cli([str(p), '--version'], home)
            h = run_cli([str(p), 'delete', '--help'], home)
            ok = v.returncode == h.returncode == 0 and 'codex-cli' in v.stdout and '--force' in h.stdout and 'UUID' in h.stdout
            if ok:
                return {'path': str(p), 'version': v.stdout.strip(), 'available': True, 'message': '官方 delete --force 能力可用'}
            errors.append(str(p) + ': 版本或 delete --force 能力验证失败')
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
    return {'path': explicit, 'version': '', 'available': False,
            'message': '只读：未找到可用 CLI。请指定桌面版 codex.exe，或加入 PATH。' + '；'.join(errors)}




class Store:
    def __init__(self, home, cli='', guard=None, runner=run_cli):
        self.home = Path(home).expanduser().resolve()
        self.cli_override = cli
        self.cli = {'available': False, 'message': '正在检测 CLI', 'path': '', 'version': ''}
        self.guard, self.runner = guard, runner
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.cancel = threading.Event()
        self.delete_stop = threading.Event()
        self.worker = None
        self.rows = {}
        self.diagnostics = []
        self.header_cache = {}
        self.status = '尚未扫描'
        self.error = ''
        self.complete = False
        self.generation = 0
        self.plans = {}
        self.current = {os.environ.get('CODEX_THREAD_ID'), os.environ.get('CODEX_SESSION_ID')}
        self.state_path = None
        self.edges = {}
        self.activity_checked_at = 0
        self.delete_result = None
        self.delete_progress = {"running": False, "phase": "idle", "can_stop": True, "done": 0, "total": 0}

    def metadata(self):
        candidates = sorted(self.home.glob('state_*.sqlite'),
                            key=lambda p: int(re.search(r'_(\d+)\.sqlite$', p.name)[1]), reverse=True)
        if not candidates:
            raise ValueError('找不到 state_*.sqlite；请检查 Codex 数据目录。无权威元信息时不从文件名猜测会话。')
        self.state_path = candidates[0]
        with db_open(self.state_path) as c:
            cols = {r['name'] for r in c.execute('PRAGMA table_info(threads)')}
            required = {'id', 'rollout_path', 'title', 'source', 'cwd', 'updated_at', 'archived'}
            if not required <= cols:
                raise ValueError('threads 表结构不兼容；只读模式，等待适配。')
            wanted = required | ({'name', 'first_user_message', 'thread_source', 'agent_path', 'history_mode', 'project_id'} & cols)
            expressions = [f'substr({k},1,24000) AS {k}' if k in ('title', 'name', 'first_user_message') else k for k in sorted(wanted)]
            rows = [dict(r) for r in c.execute('SELECT ' + ','.join(expressions) + ' FROM threads')]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            edges = list(c.execute('SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges')) if 'thread_spawn_edges' in tables else []
            self.edges = defaultdict(set)
            for parent, child in edges: self.edges[parent].add(child)
            children = {child for parent, child in edges}
            projects = dict(c.execute('SELECT id,name FROM projects')) if 'projects' in tables else {}
        result = {}
        for row in rows:
            try:
                source = json.loads(row.get('source', ''))
                parent = source.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                if uid(parent): self.edges[parent].add(row['id'])
            except (ValueError, AttributeError, TypeError): pass
            if not uid(row['id']):
                continue
            title = next((user_text(row.get(k)) for k in ('name', 'title', 'first_user_message') if user_text(row.get(k))), '')
            result[row['id']] = {**row, 'title': title[:200] or '标题缺失 · ' + row['id'][:8],
                                 'title_missing': not bool(title), 'kind': classify(row, children),
                                 'project': projects.get(row.get('project_id')) or pathlib.PureWindowsPath(row['cwd']).name or row['cwd'],
                                 'files': [], 'missing_files': [], 'size': 0, 'reclaimable': 0, 'issues': [], 'is_current': row['id'] in self.current,
                                 'state': '当前会话 · 禁止删除' if row['id'] in self.current else ('归档' if row['archived'] else '普通')}
        for sid, row in result.items():
            if row['kind'] != 'internal': continue
            review = row.get('thread_source') == 'guardian_review' or 'guardian' in row.get('source', '').lower()
            row['record_type'] = '操作安全审批' if review else '子代理任务'
            row['parents'] = [{'id': parent, 'title': result[parent]['title']} for parent, children in self.edges.items() if sid in children and parent in result]
            if row['title_missing'] or review:
                context = row['parents'][0]['title'] if row['parents'] else row['id']
                row['title'] = row['record_type'] + ' · ' + context[:140]
        return result

    def safe_file(self, path):
        p = canonical(path)
        # A reparse-point root must not make an unrelated directory eligible.
        return any(p.is_relative_to(self.home / folder) for folder in ('sessions', 'archived_sessions')) and p.suffix.lower() == '.jsonl'

    def header(self, path, stat):
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self.header_cache:
            with path.open('rb') as f:
                raw = f.readline(HEADER_LIMIT + 1)
            try:
                data = json.loads(raw) if len(raw) <= HEADER_LIMIT else {}
                meta = data.get('payload', {}) if data.get('type') == 'session_meta' else {}
                self.header_cache[key] = (uid(meta.get('id')), 'session_meta.id' if uid(meta.get('id')) else '文件头缺失/过大/无有效 id')
            except (ValueError, TypeError):
                self.header_cache[key] = (None, '文件头无法解析')
        return self.header_cache[key]

    def start_scan(self):
        if not self.operation.acquire(False):
            raise ValueError('删除操作进行中，请稍后刷新')
        try:
            if self.worker and self.worker.is_alive():
                self.cancel.set()
                self.worker.join(10)
                if self.worker.is_alive():
                    raise ValueError('正在取消上一轮扫描，请稍后重试')
            self.cancel = threading.Event()
            self.complete = False
            self.error = ''
            self.plans.clear()
            self.worker = threading.Thread(target=self.scan, daemon=True)
            self.worker.start()
        finally:
            self.operation.release()

    def scan(self, detect=True):
        try:
            self.status = '读取会话元信息'
            rows = self.metadata()
            with self.lock:
                self.rows = rows
                self.diagnostics = []
                self.generation += 1
            if detect:
                self.cli = detect_cli(self.home, self.cli_override)
            refs = defaultdict(set)
            for sid, row in rows.items():
                p = canonical(row['rollout_path'])
                refs[str(p)].add(sid)
                if not self.safe_file(p):
                    row['issues'].append('数据库引用路径超出 sessions/archived_sessions，禁止操作')
            paths = set(refs)
            for folder in ('sessions', 'archived_sessions'):
                root = self.home / folder
                if root.exists():
                    paths.update(str(canonical(p)) for p in root.rglob('*.jsonl'))
            seen = {}
            for i, name in enumerate(sorted(paths)):
                if self.cancel.is_set():
                    self.status = '已取消；统计不完整，禁止删除'; return
                self.status = f'扫描文件属性与有限文件头 {i + 1} / {len(paths)}'
                p = Path(name)
                if not self.safe_file(p):
                    self.diagnostics.append({'path': name, 'reason': '越界路径 / 符号链接', 'size': 0, 'kind': 'unsafe', 'thread_ids': sorted(refs[name])}); continue
                try:
                    st = p.stat()
                    owner, evidence = self.header(p, st)
                except OSError as e:
                    reason = '文件缺失：' if isinstance(e, FileNotFoundError) else '文件读取失败：'
                    for sid in refs[name]:
                        rows[sid]['issues'].append(reason + name)
                        if isinstance(e, FileNotFoundError): rows[sid]['missing_files'].append(name)
                    self.diagnostics.append({'path': name, 'reason': str(e), 'size': 0, 'kind': 'missing' if isinstance(e, FileNotFoundError) else 'unreadable', 'thread_ids': sorted(refs[name])}); continue
                claims = set(refs[name])
                if owner in rows: claims.add(owner)
                conflict = bool(owner and owner not in rows and claims)
                identity = (st.st_dev, st.st_ino)
                hardlink = st.st_nlink > 1
                if identity in seen:
                    hardlink = True
                seen[identity] = name
                item = {'path': name, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                        'identity': [st.st_dev, st.st_ino],
                        'evidence': evidence + (' + threads.rollout_path' if refs[name] else ''),
                        'shared': len(claims) > 1 or conflict or hardlink}
                if not claims:
                    self.diagnostics.append({**item, 'reason': '无权威会话归属；不使用文件名 UUID 猜测', 'kind': 'orphan', 'thread_ids': [], 'owner': owner}); continue
                for sid in claims:
                    row = rows[sid]
                    with self.lock:
                        row['files'].append(dict(item))
                        if item['shared']:
                            if len(claims) > 1 or conflict: row['issues'].append('归属冲突：' + name)
                            if hardlink: row['issues'].append('硬链接文件：' + name)
                        else:
                            row['size'] += st.st_size
                            row['reclaimable'] += st.st_size
            for row in rows.values():
                if self.cancel.is_set():
                    self.status = '已取消；统计不完整，禁止删除'; return
                if row['title_missing'] and row['kind'] == 'main':
                    try:
                        preview = self.preview(row['id'], 0)
                        text = next((x['text'] for x in preview['messages'] if x['role'] == 'user'), '')
                        if text: row['title'] = text[:120]
                    except (OSError, sqlite3.Error, ValueError):
                        pass
            self.complete = True
            self.status = f'扫描完成 · {len(paths)} 个文件'
        except Exception as e:
            self.complete = False
            self.error = str(e)
            self.status = '扫描失败'

    def snapshot(self):
        if time.monotonic() - self.activity_checked_at > 3:
            with self.lock:
                for sid, row in self.rows.items():
                    activity = self.activity_for(sid)
                    row['activity'] = activity
                    row['state'] = '当前会话 · 禁止删除' if sid in self.current else ('归档' if row['archived'] else '普通') + ' · ' + activity['label']
                self.activity_checked_at = time.monotonic()
        with self.lock:
            return {'rows': [{**{k: v for k, v in row.items() if k not in ('files', 'first_user_message')}, 'file_count': len(row['files'])} for row in self.rows.values()],
                    'spawn_edges': {parent: sorted(children) for parent, children in self.edges.items()},
                    'diagnostics': self.diagnostics[:1000], 'diagnostic_count': len(self.diagnostics),
                    'status': self.status, 'complete': self.complete, 'error': self.error,
                    'cli': self.cli, 'home': str(self.home), 'generation': self.generation,
                    'source': '元信息只读 · 会话写入锁实时检测（约每 6 秒更新）'}

    def history_remaining(self, sid):
        path = self.home / 'thread_history_1.sqlite'
        if not path.exists(): return {}
        with db_open(path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: bool(c.execute(f'SELECT EXISTS(SELECT 1 FROM {table} WHERE thread_id=?)', (sid,)).fetchone()[0])
                    for table in ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items') if table in tables}

    def preview(self, sid, offset=0, file_index=0):
        if sid not in self.rows: raise ValueError('会话不存在')
        offset = max(0, min(int(offset), 2**63 - 1))
        row = self.rows[sid]
        internal = row['kind'] == 'internal'
        history = self.home / 'thread_history_1.sqlite'
        # A resumed session can span several rollouts; the projection may cover only one.
        if len([f for f in row['files'] if not f['shared']]) > 1:
            return self.legacy_preview(row, offset, file_index)
        if row.get('history_mode') == 'paginated' and history.exists():
            with db_open(history) as c:
                records = c.execute('''SELECT i.item_id, i.item_type, substr(i.item_json,1,65536) AS body,
                    length(i.item_json) AS n, t.final_agent_item_id
                    FROM thread_items i LEFT JOIN thread_turns t ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id
                    WHERE i.thread_id=? AND i.item_type IN ('userMessage','agentMessage')
                    ORDER BY i.rollout_ordinal,i.item_id LIMIT 41 OFFSET ?''', (sid, offset)).fetchall()
            messages = []
            for r in records[:40]:
                if r['n'] > 65536:
                    if r['item_type'] == 'userMessage' or r['item_id'] == r['final_agent_item_id']:
                        messages.append({'role': 'user' if r['item_type'] == 'userMessage' else 'assistant', 'text': '[超大消息已省略，可能包含图片]', 'label': '大小限制'})
                    continue
                try: item = json.loads(r['body'])
                except ValueError: continue
                if r['item_type'] == 'userMessage':
                    text = blocks_text(item.get('content')) if internal else user_text(blocks_text(item.get('content')))
                    if text: messages.append({'role': 'user', 'text': text, 'label': '用户'})
                elif internal or item.get('phase') in ('final', 'final_answer') or r['item_id'] == r['final_agent_item_id']:
                    text = clean_text(item.get('text'))
                    if internal or not self.audit(text): messages.append({'role': 'assistant', 'text': text, 'label': '最终回复'})
            return {'messages': messages, 'next': offset + 40 if len(records) > 40 else None,
                    'note': ('内部记录：任务输入与审批 / 代理回复。' if internal else '分页预览：仅显示用户输入与已确认的最终回复。') + '每页最多检查 40 条，超大消息显示占位。', 'files': row['files'], 'file_index': 0}
        return self.legacy_preview(row, offset, file_index)

    @staticmethod
    def audit(text):
        return all(k in text for k in ('risk_level', 'user_authorization', 'outcome', 'rationale'))

    def legacy_preview(self, row, offset, file_index):
        files = [x for x in row['files'] if not x['shared']]
        if not files or file_index < 0 or file_index >= len(files):
            return {'messages': [], 'next': None, 'note': '没有可安全预览的关联文件', 'files': row['files']}
        path = Path(files[file_index]['path'])
        if not self.safe_file(path): raise ValueError('文件越界')
        messages = []
        internal = row['kind'] == 'internal'
        used = 0
        with path.open('rb') as f:
            # Offset can be inside an oversized record: discard its suffix in bounded chunks.
            f.seek(min(offset, path.stat().st_size))
            discard = False
            if offset:
                f.seek(offset - 1); discard = f.read(1) != b'\n'
            while used < PAGE_BYTES and len(messages) < 40:
                raw = f.readline(min(RECORD_LIMIT + 1, PAGE_BYTES - used))
                if not raw: break
                used += len(raw)
                if discard or len(raw) > RECORD_LIMIT or not raw.endswith(b'\n'):
                    discard = not raw.endswith(b'\n'); continue
                try: event = json.loads(raw)
                except ValueError: continue
                p = event.get('payload', {})
                if event.get('type') == 'response_item' and p.get('type') == 'message':
                    role = p.get('role')
                    text = blocks_text(p.get('content'))
                    label = '用户'
                    if role == 'user': text = text if internal else user_text(text)
                    elif role == 'assistant' and (internal or p.get('phase') in ('final', 'final_answer')): label = '审批结果 / 代理回复' if internal else '最终回复'
                    else: continue
                    if text and (internal or not self.audit(text)): messages.append({'role': role, 'text': text, 'label': label})
            pos = f.tell()
        more = pos < path.stat().st_size
        next_file = file_index if more else file_index + 1
        return {'messages': messages, 'next': pos if more else (0 if next_file < len(files) else None),
                'file_index': next_file, 'files': row['files'],
                'note': ('内部记录：任务输入与审批 / 代理回复。' if internal else '缺少 final 标记的助手消息隐藏。') + f'正在读取日志 {file_index + 1} / {len(files)}；每次最多 4 MiB，单条 256 KiB。'}

    def activity_for(self, sid):
        # Optional injection is used only by isolated tests, never HTTP settings.
        if self.guard is not None:
            reason = self.guard()
            return {'code': 'unknown' if reason else 'idle', 'label': '状态未确认' if reason else '未占用', 'reason': reason}
        return thread_activity(self.home, sid, self.cli.get('version', ''))

    def expand_ids(self, roots, include_missing=False):
        result = []
        pending = list(roots)
        seen = set()
        while pending:
            sid = pending.pop(0)
            if sid in seen: continue
            seen.add(sid)
            if sid in self.rows or include_missing:
                result.append(sid)
            pending.extend(sorted(self.edges.get(sid, set())))
        return result

    def missing_descendants(self, roots):
        return sorted(set(self.expand_ids(roots, include_missing=True)) - self.rows.keys())

    def missing_descendant_blockers(self, ids):
        blockers = []
        for sid in ids:
            if sid in self.current:
                blockers.append('当前会话永久受保护：' + sid)
            reason = self.activity_for(sid)['reason']
            if reason: blockers.append(reason + '：' + sid)
            if any(d.get('owner') == sid for d in self.diagnostics):
                blockers.append('子会话元信息缺失但仍有关联文件，需先核实：' + sid)
        return blockers

    @staticmethod
    def blocking_issues(row):
        # Confirmed absence is a cleanup candidate, not a permission/ownership error.
        return [issue for issue in row['issues'] if not issue.startswith('文件缺失：')]

    def plan(self, ids):
        if self.operation.locked(): raise ValueError('删除或扫描正在进行，请等待完成')
        if not self.complete: raise ValueError('需等待完整扫描后生成删除计划')
        ids = list(dict.fromkeys(ids))
        if not ids or any(not uid(x) or x not in self.rows for x in ids):
            raise ValueError('请至少选择一个有效会话')
        reasons = []
        if not self.cli['available']: reasons.append(self.cli['message'])
        requested = list(ids)
        descendants = {child for sid in requested for child in self.expand_ids([sid])[1:]}
        roots = [sid for sid in requested if sid not in descendants]
        if not roots: raise ValueError('子会话关系存在循环，无法确认删除范围')
        ids = self.expand_ids(roots)
        missing = self.missing_descendants(roots)
        reasons.extend(self.missing_descendant_blockers(missing))
        entries = []
        for sid in ids:
            row = self.rows[sid]
            blockers = self.blocking_issues(row)
            activity = self.activity_for(sid)
            if activity['reason']: blockers.append(activity['reason'])
            if sid in self.current: blockers.append('当前会话永久受保护，不能由本工具删除')
            if row['kind'] == 'unknown': blockers.append('无法判断是否为用户会话')
            entries.append({'id': sid, 'title': row['title'], 'files': row['files'],
                            'missing_files': list(row['missing_files']),
                            'warnings': ['文件已缺失，仅清理残留记录；缺失文件不计入释放空间：' + p for p in row['missing_files']],
                            'bytes': row['reclaimable'], 'blockers': blockers, 'activity': activity,
                            'included_descendant': sid not in roots})
        token = uuid.uuid4().hex
        plan = {'token': token, 'entries': entries, 'blockers': reasons, 'roots': roots,
                'bytes': sum(e['bytes'] for e in entries), 'generation': self.generation, 'missing_descendants': missing,
                'expires': time.time() + 300, 'allowed': not reasons and not any(e['blockers'] for e in entries)}
        self.plans[token] = plan
        return plan

    def stop_delete(self):
        if self.delete_progress.get('running'):
            self.delete_stop.set()
            self.delete_progress = {**self.delete_progress, 'stop_requested': True}
        return {'ok': True, 'running': self.delete_progress.get('running', False)}

    def check_write_access(self):
        try:
            # Request write access without changing database contents.
            with self.state_path.open('r+b'): pass
            folders = [self.home]
            for folder in (self.home / 'tmp', self.home / 'tmp' / 'arg0'):
                if folder.is_dir(): folders.append(folder)
            for folder in folders:
                with tempfile.TemporaryFile(prefix='cleaner-permission-', dir=folder) as probe:
                    probe.write(b'permission check')
                    probe.flush()
        except OSError as e:
            raise ValueError('清理服务没有数据目录写入权限，尚未执行删除。请关闭此服务后，从资源管理器运行项目中的 start.cmd。原始错误：' + str(e)) from e

    def delete(self, token):
        if not self.operation.acquire(False): raise ValueError('已有删除操作正在执行')
        try:
            plan = self.plans.pop(token, None)
            if not plan or plan['expires'] < time.time() or plan['generation'] != self.generation:
                raise ValueError('计划已使用、过期或数据已刷新，请重新生成')
            if not plan['allowed'] or not self.complete: raise ValueError('删除计划存在阻止条件')
            self.delete_stop.clear()
            self.delete_result = None
            self.delete_progress = {'running': True, 'phase': '核实删除范围', 'can_stop': True, 'done': 0, 'total': len(plan['entries']), 'started': time.time(), 'title': ''}
            self.check_write_access()
            current_rows = self.metadata()
            fresh = Store(self.home, self.cli_override, guard=self.guard, runner=self.runner)
            fresh.cli = dict(self.cli)
            fresh.current = set(self.current)
            fresh.header_cache = dict(self.header_cache)
            fresh.scan(detect=False)
            if not fresh.complete: raise ValueError('删除前重新核实失败：' + fresh.error)
            if set(fresh.expand_ids(plan['roots'])) != {e['id'] for e in plan['entries']}:
                raise ValueError('子会话关系已变化，请重新生成计划')
            if fresh.missing_descendants(plan['roots']) != plan.get('missing_descendants', []):
                raise ValueError('子会话关系已变化，请重新生成计划')
            missing_blockers = fresh.missing_descendant_blockers(plan.get('missing_descendants', []))
            if missing_blockers: raise ValueError('；'.join(missing_blockers))
            # Validate the whole batch before executing the first official command.
            for entry in plan['entries']:
                sid = entry['id']
                reason = self.activity_for(sid)['reason']
                if reason: raise ValueError(reason)
                if sid in self.current or sid not in current_rows: raise ValueError('会话状态已变更')
                latest = fresh.rows.get(sid)
                if not latest or self.blocking_issues(latest) or latest['kind'] == 'unknown': raise ValueError('归属或元信息已变化，请刷新')
                if latest['files'] != entry['files']: raise ValueError('关联文件发生变化，请刷新后重新确认')
                if latest['missing_files'] != entry['missing_files']: raise ValueError('缺失文件状态已变化，请刷新后重新确认')
                if current_rows[sid]['rollout_path'] != self.rows[sid]['rollout_path']: raise ValueError('会话路径已改变，请刷新')
                for item in entry['files']:
                    p = Path(item['path'])
                    if not self.safe_file(p) or item['shared']: raise ValueError('文件越界或共享')
                    st = p.stat()
                    if (st.st_size, st.st_mtime_ns) != (item['size'], item['mtime_ns']): raise ValueError('会话文件已改变，请刷新')
            results = []
            for root in plan['roots']:
                if self.delete_stop.is_set():
                    finished = {r['id'] for r in results}
                    results.extend({'id': e['id'], 'title': e['title'], 'status': '未执行', 'message': '用户已停止后续删除。'} for e in plan['entries'] if e['id'] not in finished)
                    break
                self.delete_progress = {**self.delete_progress, 'phase': '调用官方 CLI', 'title': self.rows[root]['title']}
                descendants = set(fresh.expand_ids([root]))
                group = [e for e in plan['entries'] if e['id'] in descendants]
                checks = [self.activity_for(e['id']) for e in group]
                missing_blockers = fresh.missing_descendant_blockers(fresh.missing_descendants([root]))
                reason = next((a['reason'] for a in checks if a['reason']), '') or '；'.join(missing_blockers)
                if reason:
                    results.append({'id': root, 'status': '失败', 'message': reason + '；本批次停止'}); break
                command_error = ''
                code = None
                try:
                    r = self.runner([self.cli['path'], 'delete', '--force', root], self.home, timeout=120)
                    code = r.returncode
                    command_error = clean_text(r.stderr or r.stdout)[:2000]
                except (OSError, subprocess.TimeoutExpired) as e:
                    command_error = str(e)
                self.delete_progress = {**self.delete_progress, 'phase': '核实清理结果'}
                for entry in group:
                    leftovers, released = [], 0
                    for f in entry['files']:
                        if Path(f['path']).exists(): leftovers.append(f)
                        else: released += f['size']
                    for path in entry['missing_files']:
                        if Path(path).exists():
                            leftovers.append({'path': path, 'size': 0, 'evidence': 'threads.rollout_path（计划时已缺失，执行后重新出现）'})
                    try:
                        with db_open(self.state_path) as c:
                            present = bool(c.execute('SELECT EXISTS(SELECT 1 FROM threads WHERE id=?)', (entry['id'],)).fetchone()[0])
                        history_remaining = self.history_remaining(entry['id'])
                        verification = ''
                    except Exception as e:
                        present = None
                        history_remaining = {}
                        verification = str(e)
                    status = '成功' if code == 0 and present is False and not leftovers and not any(history_remaining.values()) else ('部分完成' if released or present is False else '失败')
                    results.append({'id': entry['id'], 'title': entry['title'], 'status': status,
                                    'released': released, 'metadata_present': present, 'leftovers': leftovers,
                                    'history_remaining': history_remaining,
                                    'exit_code': code, 'message': command_error, 'verification_error': verification})
                    self.delete_progress = {**self.delete_progress, 'done': len(results)}
                if code != 0 and any(marker in command_error.lower() for marker in ('failed to initialize state database', 'access is denied', 'os error 5', '拒绝访问')):
                    finished = {r['id'] for r in results}
                    for pending in plan['entries']:
                        if pending['id'] not in finished:
                            results.append({'id': pending['id'], 'title': pending['title'], 'status': '未执行', 'message': '环境或权限错误，已停止后续删除。'})
                    break
            self.complete = False
            self.status = '删除完成，请刷新重新统计'
            self.plans.clear()
            self.delete_result = {'results': results, 'note': '仅核实元信息与原关联文件。未自行删除残留，未压缩数据库；释放值为已消失文件的逻辑字节数。'}
            return self.delete_result
        except Exception as e:
            self.delete_progress = {**self.delete_progress, 'error': str(e)}
            raise
        finally:
            self.delete_progress = {**self.delete_progress, 'running': False}
            self.operation.release()
