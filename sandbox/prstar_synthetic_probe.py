"""Explicit API-only simulation; never claims to have performed a browser search."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Source/bots'))
from task_service import TaskService
from diagnostics import safe_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--polls', type=int, default=20)
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.polls <= 120 or args.interval < 10:
        parser.error('polls must be 1..120, interval must be at least 10 seconds')
    key = args.key_file.read_text().strip()
    directory = Path('sandbox/artifacts') / ('prstar-synthetic-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    client_id = 'sandbox-api-synthetic-' + uuid.uuid4().hex[:10]
    selected = {}
    print('Artifacts: ' + str(directory.resolve()), flush=True)
    with TaskService(key) as service:
        for number in range(1, args.polls + 1):
            try:
                tasks = service.fetch()
            except Exception as error:
                (directory / 'error.json').write_text(json.dumps({'operation': 'fetch', 'error': safe_error(error)}))
                return 1
            for task in tasks:
                if task.domain:
                    selected.setdefault((task.query, task.domain), task)
            entry = {'time': datetime.now(timezone.utc).isoformat(), 'attempt': number,
                     'http_status': service.last_response['http_status'],
                     'tasks': [{'query': t.query, 'domain': t.domain} for t in tasks]}
            (directory / f'fetch-{number}.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2))
            print(json.dumps({'attempt': number, 'task_count': len(tasks),
                              'usable_distinct_tasks': len(selected)}), flush=True)
            if len(selected) >= 2:
                break
            if number < args.polls:
                time.sleep(args.interval)
        if len(selected) < 2:
            print('No test report sent: fewer than two tasks with domains', flush=True)
            return 2
        stats = []
        for index, task in enumerate(list(selected.values())[:4]):
            found = index % 2 == 0
            stats.append({'domain': task.domain, 'key': task.query,
                          'page': '1' if found else 'Not found',
                          'position': '3' if found else 'Not found',
                          'date': datetime.now(timezone.utc).isoformat(), 'ip': client_id})
        record = {'synthetic': True, 'browser_run': False, 'engine': 'yandex',
                  'client_id': client_id, 'stats': stats}
        (directory / 'planned-submission.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        try:
            service.post('/help/botsStatsFile', {'stats': json.dumps(stats, ensure_ascii=False), 'fileName': client_id})
            record['http_sent'] = True
        except Exception as error:
            record['error'] = safe_error(error)
        record['response'] = None if service.last_response is None else {
            'http_status': service.last_response['http_status'],
            'body': service.last_response['body'].replace(key, '[redacted]')}
        (directory / 'submission.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return 0 if record.get('http_sent') else 1


if __name__ == '__main__':
    raise SystemExit(main())
