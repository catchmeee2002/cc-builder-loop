"""Opt-in bounded metadata evidence, independent of acceptance and run state."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import stat
import tempfile
import warnings

ENV_NAME = 'CODEX_BUILDER_LOOP_OBSERVATION_DIR'
MAX_BYTES = 16 * 1024 * 1024
FOOTER_RESERVE = 128 * 1024


class ObservationJournal:
    def __init__(self, directory: str, *, utc_anchor: str, max_bytes: int = MAX_BYTES):
        root = Path(directory)
        if not root.is_absolute():
            raise ValueError('observation directory must be absolute')
        mode = root.stat()
        if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise ValueError('observation directory must be owned by current user and private')
        fd, name = tempfile.mkstemp(prefix=f'transport-{os.getpid()}-', suffix='.jsonl', dir=root)
        self.path = Path(name)
        self.stream = os.fdopen(fd, 'w', buffering=1)
        self.max_bytes = max_bytes
        self.written = 0
        self.omitted = 0
        self.failed = False
        self.current = None
        self.finished = deque(maxlen=64)
        self.evicted_turns = 0
        self._write({'kind': 'journal_started', 'schema_version': 1, 'utc_anchor': utc_anchor})

    def mark_failed(self):
        self.failed = True
        try:
            warnings.warn('Transport observation journal failed; diagnostic evidence is incomplete', RuntimeWarning)
        except Exception:
            # A caller's warnings-as-errors policy must not prevent process cleanup.
            pass

    def bind_identity(self, *, generation, executable_identity, process_identity):
        self._write({'kind': 'transport_identity', 'generation': generation,
                     'executable_sha256': executable_identity.get('sha256'),
                     'runtime_version': executable_identity.get('runtime_version'),
                     'pid': process_identity.get('pid'), 'pgid': process_identity.get('pgid')})

    def _write(self, row, *, footer=False):
        if self.failed or self.stream.closed:
            return
        try:
            text = json.dumps(row, sort_keys=True, allow_nan=False) + '\n'
            size = len(text.encode())
            if not footer and self.written + size > self.max_bytes - FOOTER_RESERVE:
                self.omitted += 1
                return
            self.stream.write(text)
            self.written += size
        except (OSError, ValueError, TypeError):
            self.mark_failed()

    def _finish_turn(self, outcome):
        if self.current is not None:
            self.current['outcome'] = outcome
            if len(self.finished) == self.finished.maxlen:
                self.evicted_turns += 1
            self.finished.append(self.current)
            self.current = None

    def observe(self, event):
        kind = event['kind']
        if kind == 'wait_turn':
            self._finish_turn('observation_restarted')
            self.current = {key: event[key] for key in (
                'thread_id', 'turn_id', 'turn_idle_seconds', 'turn_total_seconds', 'offset_seconds')}
            self.current.update(max_complete_frame_wait_seconds=0.0, parsed_frames=0)
        elif kind == 'message_parsed' and self.current is not None:
            self.current['parsed_frames'] += 1
            duration = event.get('complete_frame_wait_seconds', 0.0)
            if duration >= self.current['max_complete_frame_wait_seconds']:
                self.current['max_complete_frame_wait_seconds'] = duration
                self.current['longest_read_frame'] = {key: event.get(key) for key in (
                    'offset_seconds', 'sequence', 'method', 'thread_id', 'turn_id')}
        elif kind in ('turn_wait_completed', 'turn_wait_failed', 'turn_total_timeout'):
            self._finish_turn(event.get('code', kind))
        self._write(event)

    def close(self, *, cleanup):
        if self.stream.closed:
            return
        self._finish_turn('transport_closed_without_wait_result')
        self._write({'kind': 'journal_closed', 'io_failed': self.failed,
                     'omitted_events': self.omitted, 'evicted_turn_summaries': self.evicted_turns,
                     'turns': list(self.finished), 'cleanup': cleanup}, footer=True)
        try:
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except OSError:
            self.mark_failed()
        finally:
            try:
                self.stream.close()
            except OSError:
                pass
