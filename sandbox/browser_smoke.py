"""Finite Selenium smoke test against a local fixture, without production tasks."""
import argparse
from pathlib import Path
import sys
from urllib.parse import quote

from selenium.webdriver import ChromeOptions, ChromeService
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Source/bots"))
from chromeBot import ChromeBot
from botAction import BotAction


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--driver", help="Optional compatible ChromeDriver path")
    args = parser.parse_args()
    options = ChromeOptions()
    if not args.headed:
        options.add_argument("--headless=new")
    options.add_argument("--disable-background-networking")
    options.add_argument("--no-first-run")
    output = ROOT / "sandbox/artifacts"
    output.mkdir(parents=True, exist_ok=True)
    service = ChromeService(executable_path=args.driver) if args.driver else ChromeService()
    driver = ChromeBot(options=options, service=service)
    try:
        driver.set_page_load_timeout(15)
        driver.set_script_timeout(15)
        fixture = """<!doctype html><meta charset="utf-8"><title>Sandbox</title>
        <label>Query <input id="query"></label>
        <button id="submit" onclick="document.querySelector('#result').textContent=document.querySelector('#query').value">Search</button>
        <p id="result"></p>"""
        driver.get("data:text/html;charset=utf-8," + quote(fixture))
        field = driver.wait_for_element(By.ID, "query", timeout=5)
        if field is None:
            raise AssertionError("Fixture input not found")
        action = BotAction(driver, 0)
        action.safe_click(field)
        action.typing("test")
        action.safe_click(driver.find_element(By.ID, "submit"))
        assert driver.find_element(By.ID, "result").text == "test"
        assert driver.safe_find_element(By.ID, "missing") is None
        assert driver.wait_for_element(By.ID, "missing", timeout=1) is None
        driver.save_screenshot(str(output / "browser-smoke.png"))
        print("PASS: original ChromeBot and BotAction on local fixture")
    except Exception:
        driver.save_screenshot(str(output / "browser-failure.png"))
        raise
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
