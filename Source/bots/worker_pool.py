"""Continuous, bounded thread pool; each browser batch runs in its own process."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from task_service import TaskService
from diagnostics import error_diagnostic, rotating_log
from artifact_storage import prune_completed

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def load_proxies(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise ValueError('Proxy file must contain a nonempty JSON array')
    result, seen = [], set()
    for proxy in data:
        if not isinstance(proxy, dict) or not isinstance(proxy.get('server'), str):
            raise ValueError('Each proxy must have a server string')
        url = urlsplit(proxy['server'])
        if url.scheme not in ('socks5', 'http', 'https') or not url.hostname or not url.port or url.username or url.password:
            raise ValueError('Proxy server requires scheme://host:port; credentials belong in separate fields')
        for field in ('username', 'password'):
            if field in proxy and not isinstance(proxy[field], str):
                raise ValueError('Proxy credentials must be strings')
        identity = (url.hostname.lower(), url.port)
        if identity in seen:
            raise ValueError('Duplicate proxy endpoint')
        seen.add(identity)
        # Profile identity contains no credentials and remains stable across cycles.
        name = hashlib.sha256(proxy['server'].encode()).hexdigest()[:16]
        result.append((name, proxy))
    return result


def run_child(command, console, stop, *, timeout=None, clock=time.monotonic):
    with console.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != 'nt')
        started = clock()
        try:
            while process.poll() is None:
                if stop.wait(0.25):
                    break
                if timeout is not None and clock() - started >= timeout:
                    raise TimeoutError('Browser batch exceeded its watchdog deadline')
            return process.poll()
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
            stream.flush()
            if console.stat().st_size > 5*1024*1024:
                with console.open('rb') as reader:
                    reader.seek(-5*1024*1024, 2)
                    tail=reader.read()
                console.write_bytes(b'[Older console output trimmed]\n'+tail)


def worker(number, proxies, stop, *, key, output, profiles, client_id,
           interval, extra, max_cycles=0, service_factory=TaskService, launch=run_child,
           artifact_max_mb=1024, artifact_max_batches=100):
    """A proxy is leased for fetching, processing, reporting and cooldown."""
    service = service_factory(key)
    cycles = 0
    try:
        while not stop.is_set() and (not max_cycles or cycles < max_cycles):
            try:
                identity, proxy = proxies.get(timeout=0.25)
            except queue.Empty:
                continue
            private_files = []
            batch = None
            try:
                cycles += 1
                stage = 'fetch_tasks'
                tasks = service.fetch()
                response = service.last_response or {}
                LOG.info('Worker %s proxy %s: received %s; HTTP %s; body length %s',
                         number, identity, len(tasks), response.get('http_status'), len(response.get('body', '')))
                if tasks:
                    stage = 'save_tasks'
                    batch = output / f'worker-{number}' / uuid.uuid4().hex
                    batch.mkdir(parents=True)
                    tasks_file = batch / 'received-tasks.json'
                    from dataclasses import asdict
                    tasks_file.write_text(json.dumps([asdict(task) for task in tasks], ensure_ascii=False, indent=2), encoding='utf-8')
                    # Private per-batch config is local and ignored, never a CLI secret.
                    proxy_file = batch / 'proxy.json'
                    private_files.append(proxy_file)
                    descriptor = os.open(proxy_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                        json.dump(proxy, stream)
                    # TaskService takes the already validated key; pass it through a private file.
                    key_file = batch / 'service-key.txt'
                    private_files.append(key_file)
                    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                        stream.write(key)
                    try:
                        # Chunk only after a nonempty server batch; no tasks are discarded by limit-tasks.
                        for offset in range(0, len(tasks), 100):
                            if stop.is_set():
                                break
                            chunk = batch / f'chunk-{offset // 100 + 1}'
                            chunk.mkdir()
                            chunk_tasks = chunk / 'tasks.json'
                            chunk_tasks.write_text(json.dumps([asdict(task) for task in tasks[offset:offset+100]], ensure_ascii=False), encoding='utf-8')
                            command = [sys.executable, '-u', str(ROOT / 'Source/bots/main.py'),
                                       *extra, '--tasks-file', str(chunk_tasks), '--limit-tasks', '100',
                                       '--service-key-file', str(key_file), '--upload-stats',
                                       '--client-id', f'{client_id}-{identity}', '--proxy-config', str(proxy_file),
                                       '--profile', str(profiles / identity), '--output', str(chunk)]
                            stage = 'launch_batch'
                            if launch is run_child:
                                timing = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
                                timing.add_argument('--deadline', type=float, default=600)
                                options, _ = timing.parse_known_args(extra)
                                # Includes launch/close overhead and per-task diagnostics.
                                watchdog = min(100, len(tasks)-offset) * (options.deadline + 90) + 180
                                code = launch(command, chunk / 'console.log', stop, timeout=watchdog)
                            else:
                                code = launch(command, chunk / 'console.log', stop)
                            LOG.info('Worker %s proxy %s: batch exit %s; logs %s', number, identity, code, chunk)
                            if code != 0:
                                break  # Failed tasks are retained, never blindly replayed or reported.
                    finally:
                        proxy_file.unlink(missing_ok=True)
                        key_file.unlink(missing_ok=True)
            except Exception as error:
                diagnostic = error_diagnostic(error, stage=stage, secrets=[key, *proxy.values()])
                diagnostic.update(worker=number, proxy_id=identity)
                error_folder = output / f'worker-{number}'
                try:
                    error_folder.mkdir(parents=True, exist_ok=True)
                    (error_folder / f'worker-error-{uuid.uuid4().hex}.json').write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError:
                    LOG.warning('Could not save worker error diagnostics')
                LOG.warning('Worker %s cycle failed at %s: %s: %s', number, stage, diagnostic['error_type'], diagnostic['message'])
            finally:
                for path in private_files:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        LOG.warning('Could not remove private configuration file: %s', path.name)
                if batch is not None:
                    try:
                        (batch/'.complete').touch()
                        removed = prune_completed(output, max_bytes=artifact_max_mb*1024*1024, max_batches=artifact_max_batches)
                        if removed:
                            LOG.info('Artifact retention: removed %s oldest completed batches', len(removed))
                    except OSError:
                        LOG.warning('Could not apply artifact retention')
                stop.wait(interval)
                proxies.put((identity, proxy))
                proxies.task_done()
    finally:
        service.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--workers', type=int, required=True)
    parser.add_argument('--proxies-file', type=Path, required=True)
    parser.add_argument('--service-key-file', type=Path, required=True)
    parser.add_argument('--captcha-key-file', type=Path, required=True)
    parser.add_argument('--task-poll-interval', type=float, default=1)
    parser.add_argument('--client-id', default='worker')
    parser.add_argument('--output', type=Path, default=Path('sandbox/artifacts/workers'))
    parser.add_argument('--profiles', type=Path, default=Path('Data/browser-profiles/workers'))
    parser.add_argument('--artifact-max-mb', type=int, default=1024)
    parser.add_argument('--artifact-max-batches', type=int, default=100)
    args, extra = parser.parse_known_args(argv)
    extra += ['--captcha-key-file', str(args.captcha_key_file.resolve())]
    if args.workers < 1 or args.workers > 100 or not math.isfinite(args.task_poll_interval) or args.task_poll_interval < 1:
        parser.error('workers must be 1..100; task-poll-interval at least 1 second')
    if not args.client_id.strip():
        parser.error('client-id must not be empty')
    if args.artifact_max_mb < 1 or args.artifact_max_batches < 1:
        parser.error('Artifact retention limits must be positive')
    reserved = ('--query', '--domain', '--tasks-file', '--fetch-tasks', '--wait-for-tasks', '--limit-tasks',
                '--upload-stats', '--proxy-config', '--no-proxy', '--profile', '--repeat', '--keep-open-on-failure', '--validate-config', '--help', '-h')
    if any(argument.split('=')[0] in reserved for argument in extra):
        parser.error('Task sources, proxy/profile, repeat and upload are controlled by the worker pool')
    try:
        items = load_proxies(args.proxies_file)
        key = args.service_key_file.read_text(encoding='utf-8').strip()
        if not key:
            raise ValueError('Empty service key')
        # Validate all browser flags before receiving a task from the backend.
        validation = subprocess.run([sys.executable, str(ROOT / 'Source/bots/main.py'), *extra, '--query', 'configuration-check', '--validate-config'],
                                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if validation.returncode:
            raise ValueError('Invalid browser flags')
    except (OSError, ValueError, TypeError):
        parser.error('Invalid proxy/key configuration')
    args.output = args.output.resolve()
    args.profiles = args.profiles.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), rotating_log(args.output / 'workers.log')])
    try:
        removed = prune_completed(args.output, max_bytes=args.artifact_max_mb*1024*1024,
                                  max_batches=args.artifact_max_batches)
        if removed:
            LOG.info('Startup artifact retention: removed %s oldest completed batches', len(removed))
    except OSError:
        LOG.warning('Could not apply startup artifact retention')
    leases = queue.Queue()
    for item in items:
        leases.put(item)
    stop = threading.Event()
    previous = {}
    for name in (signal.SIGINT, signal.SIGTERM):
        previous[name] = signal.signal(name, lambda *unused: stop.set())
    try:
        count = min(args.workers, len(items))
        LOG.info('Starting %s workers for %s proxies; continuous mode', count, len(items))
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, i+1, leases, stop, key=key, output=args.output,
                                   profiles=args.profiles, client_id=args.client_id,
                                   interval=args.task_poll_interval, extra=extra,
                                   artifact_max_mb=args.artifact_max_mb,
                                   artifact_max_batches=args.artifact_max_batches) for i in range(count)]
            finished, _ = wait(futures, return_when=FIRST_EXCEPTION)
            if any(future.exception() is not None for future in finished):
                LOG.error('A worker exited unexpectedly; stopping the remaining workers')
                stop.set()
            for future in futures:
                future.result()
    finally:
        stop.set()
        for name, handler in previous.items():
            signal.signal(name, handler)
    LOG.info('All workers stopped')
    return 0
