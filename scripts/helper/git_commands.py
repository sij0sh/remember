"""Shell-command parsing and commit evidence extraction. Read-only."""
import json
import os
import re
from pathlib import Path

from .profiles import content_text

SHA = r"[0-9a-f]{7,40}"
GIT_GLOBAL = r"(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+|--(?:no-pager|git-dir|work-tree|namespace|exec-path|config-env)(?:=\S+|\s+\S+)?\s+)*"
COMMIT_RE = re.compile(r"(?<![\w.-])git\s+" + GIT_GLOBAL + r"(commit-tree|commit)(?![\w-])")
BRACKET_RE = re.compile(r"\[[^\]\n]*?\b(" + SHA + r")\]")
COMMIT_LINE_RE = re.compile(r"(?m)^commit\s+(" + SHA + r")\b")
HEAD_RE = re.compile(r"HEAD is now at\s+(" + SHA + r")\b")
ONELINE_RE = re.compile(r"(?m)^(" + SHA + r")[ \t]+(\S.*)$")
BARE_RE = re.compile(r"(?m)^\s*(" + SHA + r")\s*$")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
HEREDOC_TARGET_RE = re.compile(r"(?:^|[;&|\n])\s*(?:cat|tee)\s*(?:>>?|)\s*([^\s;|&<>]+)\s*<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
MESSAGE_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*m|--message)(?:\s+|=)(\"(?:\\.|[^\"\\])*\"|'[^']*'|\S+)")
FILE_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*F|--file)(?:\s+|=)(\"(?:\\.|[^\"\\])*\"|'[^']*'|\S+)")
INNER_CMD_RE = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`)")
WORKDIR_RE = re.compile(r'\b(?:workdir|cwd)\\?"?\s*:\s*\\?"?((?:\\.|[^"\\])*)\\?"?')
CD_RE = re.compile(r"^(?:cd|pushd)\s+(\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s;&|]+)")


def q(value):
    """Decode one shell-quoted argument."""
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return value


def value_text(value):
    """Plain text from a recorded payload value without losing container text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = content_text(value)
        if text:
            return text
        parts = []
        for item in value:
            if isinstance(item, dict):
                inner = item.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    parts.append(content_text(inner))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    return "" if value is None else str(value)


def heredocs(text):
    """[(target path or '-', content)] for simple shell heredocs."""
    lines = text.splitlines(keepends=True)
    found = []
    i = 0
    while i < len(lines):
        target = None
        m = HEREDOC_TARGET_RE.search(lines[i])
        if m:
            target = m.group(1)
        hm = HEREDOC_RE.search(lines[i])
        if not hm:
            i += 1
            continue
        tag = hm.group(2)
        j = i + 1
        while j < len(lines) and lines[j].strip("\t").rstrip("\r\n") != tag:
            j += 1
        found.append((target or "-", "".join(lines[i + 1:j])))
        i = j + 1
    return found


def mask_heredocs(text):
    """Blank heredoc bodies so shell separators inside them cannot form commands."""
    chars = list(text)
    lines = text.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    i = 0
    while i < len(lines):
        hm = HEREDOC_RE.search(lines[i])
        if not hm:
            i += 1
            continue
        tag = hm.group(2)
        j = i + 1
        while j < len(lines) and lines[j].strip("\t").rstrip("\r\n") != tag:
            j += 1
        start = starts[i + 1] if i + 1 < len(starts) else len(text)
        end = starts[j] if j < len(starts) else len(text)
        for k in range(start, end):
            if chars[k] not in "\r\n":
                chars[k] = " "
        i = j + 1
    return "".join(chars)


def shell_segments(text):
    """[(start, end)] command spans separated by shell &&, ||, ; or newline."""
    spans = []
    start = 0
    i = 0
    quote = None
    escaped = False
    while i < len(text):
        c = text[i]
        if quote:
            if escaped:
                escaped = False
            elif c == "\\" and quote == '"':
                escaped = True
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "\n" or c == ";":
            spans.append((start, i))
            start = i + 1
            i += 1
            continue
        if c in "&|" and text[i:i + 2] in ("&&", "||"):
            spans.append((start, i))
            start = i + 2
            i += 2
            continue
        i += 1
    spans.append((start, len(text)))
    return spans


def strip_prefix(segment):
    """Remove env assignments, sudo/time wrappers and grouping punctuation."""
    segment = segment.strip()
    segment = re.sub(r"^[({]\s*", "", segment)
    while True:
        m = re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+|sudo\s+(?:-[A-Za-z]+\s+\S*\s+)*|"
                     r"command\s+|exec\s+|time\s+(?:-p\s+)?|nohup\s+)", segment)
        if not m:
            return segment
        segment = segment[m.end():]


def inner_shell_commands(text):
    """Commands quoted inside bash/sh -c, plus Codex exec cmd: "..." strings."""
    found = []
    for m in re.finditer(r"\b(?:bash|sh|zsh|dash)\s+-[A-Za-z]*c\s+(\"(?:\\.|[^\"\\])*\"|'[^']*')", text):
        found.append(q(m.group(1)))
    for m in INNER_CMD_RE.finditer(text):
        raw = m.group(1)
        if raw.startswith("`"):
            raw = raw[1:-1]
        found.append(q(raw))
    return found


def commit_invocations(text):
    """[(absolute offset)] for shell-level git commit/commit-tree calls."""
    if not text or "git" not in text:
        return []
    found = []
    for start, end in shell_segments(mask_heredocs(text)):
        raw = text[start:end]
        segment = strip_prefix(raw)
        if not segment:
            continue
        if COMMIT_RE.match(segment):
            found.append(start + raw.find(segment))
    return found


def commit_contexts(text):
    """[(offset, direct, command_text)] for each commit-producing invocation."""
    if not text:
        return []
    invocations = commit_invocations(text)
    if invocations:
        return [(offset, True, text) for offset in invocations]
    stripped = text.lstrip()
    if stripped.startswith(("rg ", "grep ", "echo ", "printf ", "cat ", "sed ", "awk ")):
        return []
    seen = set()
    for inner in inner_shell_commands(text):
        if inner in seen:
            continue
        seen.add(inner)
        contexts = commit_contexts(inner)
        if contexts:
            return [(offset, direct, inner) for offset, direct, _ in contexts]
    # Python/JS shell-out, e.g. subprocess.run(['git','commit',...]) or exec_command({cmd:...}).
    if re.search(r"\bgit\s+commit(?:-tree)?\b", text) or \
            re.search(r"['\"]git['\"]\s*,\s*['\"]commit", text):
        return [(0, False, text)]
    return []


def first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def extract_message(command, at):
    """Best-effort commit subject/message from one commit invocation segment."""
    segment = command[at:]
    for start, end in shell_segments(mask_heredocs(command)):
        if start <= at <= end:
            segment = command[at:end]
            break
    for m in MESSAGE_RE.finditer(segment):
        message = q(m.group(1)).strip()
        if message and not message.startswith(("$(", "`")):
            return message.splitlines()[0].strip()
    files = [q(m.group(1)) for m in FILE_RE.finditer(segment)]
    docs = heredocs(command)
    for name in files:
        if name == "-":
            for target, content in docs:
                if target == "-":
                    line = first_line(content)
                    if line:
                        return line
        for target, content in docs:
            if target != "-" and (target == name or target.endswith("/" + name.lstrip("./"))):
                line = first_line(content)
                if line:
                    return line
    # cat > /tmp/msg <<'EOF' ... EOF followed by git commit -F /tmp/msg
    if "git" in command[:at]:
        for target, content in docs:
            line = first_line(content)
            if line and target != "-":
                return line
    return ""


def normalize_subject(subject):
    text = str(subject or "")
    # Codex exec payloads leave escaped newlines inside the recorded JSON string.
    text = text.replace("\\r", " ").replace("\\n", "\n")
    text = text.splitlines()[0] if text else ""
    text = text.replace('\\"', '"').replace("\\\\", "\\")
    text = text.strip().strip('"').strip()
    return " ".join(text.split()).strip()


def strip_decoration(subject):
    return re.sub(r"^(?:\([^)]*\)|\*)\s*", "", str(subject or "").strip()).strip()


def subjects_match(left, right):
    left = normalize_subject(left)
    right = normalize_subject(right)
    if not left or not right:
        return False
    left = re.sub(r"^(?:\([^)]*\)|\*)\s*", "", left)
    right = re.sub(r"^(?:\([^)]*\)|\*)\s*", "", right)
    return left == right or left.startswith(right) or right.startswith(left)


def output_shas(output, strong_only=False):
    """[(sha, basis, subject, offset)] strongest category first."""
    if not output:
        return []
    found = []
    for m in BRACKET_RE.finditer(output):
        tail = output[m.end():].splitlines()[0] if output[m.end():] else ""
        found.append((m.group(1), "commit_output", strip_decoration(tail), m.start()))
    if found:
        return found
    if strong_only:
        return []
    for m in HEAD_RE.finditer(output):
        found.append((m.group(1), "head", "", m.start()))
    if found:
        return found
    for m in COMMIT_LINE_RE.finditer(output):
        block = output[m.end():m.end() + 2000].replace("\\n", "\n").splitlines()
        tail = next((line.strip() for line in block[1:]
                     if line.startswith(("    ", "\t")) and line.strip()), "")
        found.append((m.group(1), "log", strip_decoration(tail), m.start()))
    if found:
        return found
    for m in ONELINE_RE.finditer(output):
        found.append((m.group(1), "oneline", strip_decoration(m.group(2)), m.start()))
    if found:
        return found
    for m in BARE_RE.finditer(output):
        found.append((m.group(1), "rev_parse", "", m.start()))
    return found


def assign_shas(contexts, candidates):
    """Map commit invocations to output SHAs, preferring exact subject matches."""
    assigned = [None] * len(contexts)
    if not contexts or not candidates:
        return assigned
    messages = [normalize_subject(extract_message(text, offset)) for offset, _, text in contexts]
    used = set()
    for i, message in enumerate(messages):
        if not message:
            continue
        for j, candidate in enumerate(candidates):
            if j in used:
                continue
            if subjects_match(candidate[2], message):
                assigned[i] = candidate
                used.add(j)
                break
    remaining_contexts = [i for i, value in enumerate(assigned) if value is None]
    remaining_candidates = [j for j in range(len(candidates)) if j not in used]
    if not remaining_contexts or not remaining_candidates:
        return assigned
    # Oneline/log SHAs are only trustworthy for this commit when their subject agrees.
    for i in remaining_contexts:
        message = messages[i]
        if not message:
            allowed = remaining_candidates
        else:
            allowed = [j for j in remaining_candidates
                       if candidates[j][1] not in ("oneline", "log") or subjects_match(candidates[j][2], message)]
        if not allowed:
            continue
        # git log/oneline lists newest first; command order is oldest first.
        newest_first = candidates[allowed[0]][1] in ("oneline", "log", "rev_parse")
        ordered = list(reversed(allowed)) if newest_first else allowed
        j = ordered[0]
        assigned[i] = candidates[j]
        remaining_candidates.remove(j)
        if not remaining_candidates:
            break
    return assigned


def resolve_path(value, base):
    if not value:
        return base
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute() and base:
        path = Path(base) / path
    return str(path)


def command_cwd(command, root, before=None):
    """Last cd/pushd or Codex workdir before the commit, resolved when possible."""
    hint = None
    for m in WORKDIR_RE.finditer(command):
        if before is None or m.start() <= before:
            hint = resolve_path(q(m.group(1)).strip('\\"'), root)
    current = root
    for start, end in shell_segments(mask_heredocs(command)):
        if before is not None and start > before:
            break
        segment = strip_prefix(command[start:end])
        m = CD_RE.match(segment)
        if m:
            current = resolve_path(q(m.group(1)), current)
            hint = current
        gm = re.search(r"\bgit\s+-C\s+(\"(?:\\.|[^\"\\])*\"|'[^']*'|\S+)", segment)
        if gm:
            hint = resolve_path(q(gm.group(1)), current)
    return hint or root
