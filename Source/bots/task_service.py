"""Explicit adapter for the original task/statistics protocol."""
import json

import requests

from runner import Task


class TaskService:
    def __init__(self, key, base_url="https://prstar.ru", timeout=(5, 15)):
        if not key:
            raise ValueError("BOT_SERVICE_KEY is required")
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.last_response = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def post(self, path, fields):
        self.last_response = None
        fields = {"botsKey": self.key, "engine": "yandex", **fields}
        response = self.session.post(self.base_url + path,
                                     files={key: (None, str(value)) for key, value in fields.items()},
                                     timeout=self.timeout, allow_redirects=False)
        self.last_response = {"http_status": response.status_code, "body": response.text}
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("Task service redirect is not accepted")
        return response.text

    def fetch(self):
        text = self.post("/help/botsSreetsFile", {})
        tasks = []
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split(" | ", 1)
            tasks.append(Task(fields[0].strip(), fields[1].strip() if len(fields) == 2 else None))
        return tasks

    def submit(self, results, client_id):
        if not client_id:
            raise ValueError("A non-secret client_id is required for statistics")
        # CAPTCHA/HTTP failures are not equivalent to a missing website.
        stats = []
        for result in results:
            if result.domain is None or result.status not in ("found", "not_found"):
                continue
            if result.status == "not_found" and not (result.search_complete and
                    result.completion_reason in ("page_limit", "end_of_results")):
                continue
            stats.append({"domain": result.domain, "page": str(result.page) if result.page else "Not found",
                          "position": str(result.position) if result.position else "Not found",
                          "date": result.timestamp, "key": result.query, "ip": client_id})
        if not stats:
            return False
        self.post("/help/botsStatsFile", {"stats": json.dumps(stats, ensure_ascii=False), "fileName": client_id})
        return True
