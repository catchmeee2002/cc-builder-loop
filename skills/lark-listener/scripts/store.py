"""Private, atomic local facts. Network operations never hold this file lock."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

class ListenerError(Exception):
    def __init__(self, code, status='ERROR'):
        super().__init__(code)
        self.code, self.status = code, status

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def now():
    return time.time()

def process_start(pid):
    try:
        # comm may contain spaces or parentheses; field 22 follows its last ')'.
        fields = Path('/proc', str(int(pid)), 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, ValueError, IndexError):
        return None

def alive(p):
    return bool(p and process_start(p.get('pid')) == p.get('starttime') and p.get('starttime'))

class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ListenerError('STATE_SYMLINK')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.resolve() != self.root:
            raise ListenerError('STATE_ANCESTOR_SYMLINK')
        if self.root.stat().st_uid != os.getuid():
            raise ListenerError('STATE_OWNER')
        os.chmod(self.root, 0o700)
        self.path = self.root / 'state.json'
        self.lock = self.root / 'state.lock'

    def open_private(self, path, flags):
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid():
            os.close(fd)
            raise ListenerError('STATE_FILE_UNSAFE')
        os.fchmod(fd, 0o600)
        return fd

    @contextlib.contextmanager
    def transaction(self):
        fd = self.open_private(self.lock, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.path.exists() or self.path.is_symlink():
                readfd = self.open_private(self.path, os.O_RDONLY)
                with os.fdopen(readfd) as f:
                    data = json.load(f)
            else:
                data = {'schema_version': 1, 'subscriptions': {}, 'messages': {}, 'deliveries': {}, 'sends': {}, 'batches': {}}
            data.setdefault('batches', {})
            yield data
            tempfd, name = tempfile.mkstemp(prefix='.state-', dir=self.root)
            try:
                with os.fdopen(tempfd, 'w') as f:
                    json.dump(data, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, self.path)
                directory = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def worker_lock(self, sid):
        fd = self.open_private(self.root / ('worker-' + digest(sid) + '.lock'), os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ListenerError('WORKER_ALREADY_RUNNING')
            yield
        finally:
            os.close(fd)

    def subscription(self, data, request, owner=False):
        sid = request.get('subscription_id')
        if sid not in data['subscriptions']:
            raise ListenerError('SUBSCRIPTION_NOT_FOUND')
        s = data['subscriptions'][sid]
        if owner and request.get('owner_session') != s['owner_session']:
            raise ListenerError('OWNER_MISMATCH')
        return s
