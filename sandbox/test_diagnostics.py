import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Source/bots"))
from diagnostics import NetworkJournal, safe_error, safe_url
from runner import BrowserSession


def test_navigation_url_redacts_credentials_and_captcha_tokens():
    value = safe_url("https://user:secret@ya.ru/showcaptcha?text=test&lr=213&mt=private-token&retpath=private#secret")
    assert value == "https://ya.ru/showcaptcha?text=test&lr=213"
    assert safe_error(Exception("net::ERR_TIMED_OUT at https://user:secret@host")) == "net::ERR_TIMED_OUT"


def test_journal_records_redirect_status_without_headers(tmp_path):
    context = Mock()
    journal = NetworkJournal(context, tmp_path / "network.jsonl")
    request = SimpleNamespace(url="https://ya.ru/search?text=test&token=private", method="GET",
                              resource_type="document", redirected_from=SimpleNamespace(url="https://ya.ru/showcaptcha?mt=private"),
                              is_navigation_request=lambda: True)
    journal.request(request)
    journal.response(SimpleNamespace(request=request, status=429))
    journal.close()
    rows = [json.loads(line) for line in (tmp_path / "network.jsonl").read_text().splitlines()]
    assert rows[-1]["http_status"] == 429
    assert rows[-1]["redirected_from"] == "https://ya.ru/showcaptcha"
    assert "private" not in json.dumps(rows)
    assert context.remove_listener.call_count == 3


def test_profile_rejects_an_unrelated_chrome_directory(tmp_path):
    (tmp_path / "existing-file").write_text("user data")
    with pytest.raises(ValueError):
        BrowserSession(profile=tmp_path).prepare_profile()
    assert (tmp_path / "existing-file").read_text() == "user data"


def test_profile_is_bound_to_proxy_identity(tmp_path):
    BrowserSession(profile=tmp_path).prepare_profile()
    with pytest.raises(ValueError):
        BrowserSession(profile=tmp_path, proxy={"server": "http://other.invalid:8000"}).prepare_profile()
