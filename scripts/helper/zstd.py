"""Decompression for compressed recordings: one decoder owner, no filesystem writes.

DeepSeek Harness writes ``session.vN.jsonl.zstd``. The decoder is resolved once per
call: the 3.14+ standard library, then the optional ``zstandard`` package, then the
``zstd`` executable. A caller that finds no decoder reports the gap instead of
silently skipping the archive.
"""
import shutil
import subprocess

SUFFIX = ".jsonl.zstd"
CHUNK = 65536


def compressed(path):
    """True for a compressed recording artifact, by its suffix."""
    return str(path).endswith(SUFFIX)


def recording_name(name):
    """True for a candidate recording file name: plain or compressed JSONL."""
    return name.endswith(".jsonl") or name.endswith(SUFFIX)


def decoder_kind():
    """'stdlib', 'module', 'cli' or None; never imports the optional package twice."""
    try:
        from compression import zstd as stdlib
    except (ImportError, AttributeError):
        stdlib = None
    if stdlib is not None and hasattr(stdlib, "open"):
        return "stdlib"
    try:
        import zstandard  # noqa: F401
    except ImportError:
        pass
    else:
        return "module"
    return "cli" if shutil.which("zstd") else None


def _reader(path):
    """A binary reader over decompressed bytes, or None when no decoder exists."""
    kind = decoder_kind()
    if kind == "stdlib":
        from compression import zstd as stdlib
        return stdlib.open(path, "rb")
    if kind == "module":
        import zstandard
        return zstandard.open(path, "rb")
    if kind == "cli":
        process = subprocess.Popen(["zstd", "-dc", "--", str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return _ProcessReader(process)
    return None


class _ProcessReader:
    """The zstd process's stdout, closed with the process it belongs to."""

    def __init__(self, process):
        self.process = process

    def read(self, size=-1):
        data = self.process.stdout.read(size)
        if not data and size != 0:
            # stdout is empty only at EOF: a nonzero status means the frame is unreadable.
            code = self.process.wait()
            if code:
                raise OSError(f"zstd exited with status {code}")
        return data

    def close(self):
        try:
            if self.process.stdout is not None:
                self.process.stdout.close()
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()


class LineStream:
    """A bounded file-like view of decompressed bytes: readline, read, tell, peek, close.

    Offsets are decompressed offsets. A read failure ends the stream as an error
    the caller reports, rather than raising after partial evidence was yielded.
    """

    def __init__(self, reader, close):
        self._reader = reader
        self._close = close
        self._buffer = b""
        self._offset = 0
        self._eof = False
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._close()

    def tell(self):
        return self._offset

    def _fill(self):
        if self._eof or self.error:
            return False
        try:
            chunk = self._reader.read(CHUNK)
        except Exception as error:  # Each decoder raises its own type; all mean unreadable data.
            self.error = f"decompression_failed: {error}"
            self._eof = True
            return False
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    def peek(self, size=1):
        while len(self._buffer) < size and self._fill():
            pass
        return self._buffer[:size]

    def read(self, size=-1):
        while size < 0 and self._fill():
            pass
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            while len(self._buffer) < size and self._fill():
                pass
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        self._offset += len(data)
        return data

    def readline(self, size=-1):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                cut = newline + 1
            elif 0 <= size <= len(self._buffer):
                cut = size
            elif self._eof or self.error:
                cut = len(self._buffer)
            else:
                self._fill()
                continue
            if size >= 0:
                cut = min(cut, size)
            data, self._buffer = self._buffer[:cut], self._buffer[cut:]
            self._offset += len(data)
            return data


def open_stream(path):
    """A LineStream over a compressed recording, or None when no decoder is available."""
    reader = _reader(path)
    if reader is None:
        return None
    return LineStream(reader, reader.close)
