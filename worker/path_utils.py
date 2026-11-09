"""Shared path validation for generated files and workspace downloads."""
import re
from pathlib import PurePosixPath


def safe_path(value):
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or not path.parts or path.name in ('', '.'):
        raise ValueError('Unsafe output filename')
    if len(path.parts) > 1 and re.match(r'^[a-zA-Z]:$', path.parts[0]):
        raise ValueError('Unsafe output filename')
    return path
