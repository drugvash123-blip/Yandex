"""Retention for completed generated batches; never deletes active batches."""
from pathlib import Path
import re
import shutil
import threading

LOCK = threading.Lock()


def prune_completed(root, *, max_bytes=1024*1024*1024, max_batches=100):
    with LOCK:
        batches=[]
        for directory in Path(root).glob('worker-*/*'):
            if not re.fullmatch(r'[0-9a-f]{32}', directory.name):
                continue
            marker=directory/'.complete'
            if not marker.exists():
                # Recognize completed batches from older releases, but never
                # touch a leased proxy or an unfinished child.
                if (directory/'proxy.json').exists() or (directory/'service-key.txt').exists():
                    continue
                chunks=list(directory.glob('chunk-*'))
                if not chunks or not all(any(c.glob('submission-*.json')) or any(c.glob('batch-error-*.json')) for c in chunks):
                    continue
            size=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
            batches.append(((marker if marker.exists() else directory).stat().st_mtime, directory, size))
        batches.sort()
        total=sum(size for _,_,size in batches)
        removed=[]
        count=len(batches)
        for _, directory, size in batches:
            if total <= max_bytes and count <= max_batches:
                break
            shutil.rmtree(directory)
            total-=size; count-=1; removed.append(directory.name)
        return removed
