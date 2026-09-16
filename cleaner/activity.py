"""Read-only Windows probes of Codex's cross-process writer-lock namespace.

No lock file is created, changed or removed. A shared nonblocking byte-range
probe conflicts with the exclusive lock held by Rust std::fs::File::try_lock.
This is a snapshot only; the official CLI acquires the final exclusive lock.
"""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

VERIFIED_VERSION = 'codex-cli 0.154.0-alpha.6.2'


def probe_file(path):
    """Return free, busy, missing, or unknown. Never opens with write access."""
    if os.name != 'nt':
        return 'unknown'
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]

    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return 'missing' if ctypes.get_last_error() in (2, 3) else 'unknown'
    try:
        ov = Overlapped()
        if not kernel.LockFileEx(handle, 1, 0, 0xffffffff, 0xffffffff, ctypes.byref(ov)):
            return 'busy' if ctypes.get_last_error() == 33 else 'unknown'
        kernel.UnlockFileEx(handle, 0, 0xffffffff, 0xffffffff, ctypes.byref(ov))
        return 'free'
    finally:
        kernel.CloseHandle(handle)


def thread_activity(home, sid, version):
    if version != VERIFIED_VERSION:
        return {'code': 'unknown', 'label': '状态未确认', 'reason': '此 CLI 版本的会话锁协议尚未验证，禁止删除该目标'}
    root = Path(home) / 'thread-writer-locks'
    try:
        if not root.is_dir() or root.is_symlink() or root.resolve().parent != Path(home).resolve():
            raise OSError('writer-lock namespace unavailable')
        # The coordination file demonstrates this home uses the verified protocol.
        if not (root / '.coordination.lock').is_file():
            raise OSError('coordination marker absent')
        path = root / f'{sid}.lock'
        if path.is_symlink() or path.resolve().parent != root.resolve():
            raise OSError('writer-lock path outside namespace')
        status = probe_file(path)
    except OSError:
        status = 'unknown'
    if status == 'busy':
        return {'code': 'busy', 'label': '占用中', 'reason': '该会话仍被 Codex 持有写入锁；请先释放这个会话，无需关闭其他会话'}
    if status in ('free', 'missing'):
        return {'code': 'idle', 'label': '未占用', 'reason': ''}
    return {'code': 'unknown', 'label': '状态未确认', 'reason': '无法读取该会话的写入锁，暂不允许删除该目标'}
