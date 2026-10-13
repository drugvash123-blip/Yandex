# Sandbox

Небольшая среда для проверки legacy-кода без запуска бесконечного main.py.
Это изоляция Python-зависимостей и сценариев, не системная песочница безопасности.

## Подготовка

Из корня репозитория, Python 3.11:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r sandbox/requirements.txt
```

После обновления зависимостей сохранить проверенные версии:

```sh
.venv/bin/python -m pip freeze > sandbox/requirements.lock.txt
```

## Проверки исходной логики

```sh
.venv/bin/python -m pytest sandbox/test_legacy.py -q -rx
```

Сеть заблокирована в fixture; используются временный ключ-заглушка и временный лог.
Тесты вызывают методы исходного Bot с подставным драйвером. Они не проверяют
актуальную разметку Яндекса, совместимость Chrome или работоспособность CAPTCHA.
Три xfail фиксируют известные дефекты: общую статистику, прокрутку вверх,
совпадение домена по подстроке. После исправления соответствующий xfail нужно убрать.

## Legacy browser smoke

```sh
.venv/bin/python sandbox/browser_smoke.py
.venv/bin/python sandbox/browser_smoke.py --headed
```

Конечный сценарий открывает HTML fixture через data URL, проверяет ввод,
клик и ожидания через исходные ChromeBot/BotAction, сохраняет скриншот,
закрывает браузер в finally. Прокси, рабочие ключи и сервер заданий не используются.
Chrome должен быть установлен. Selenium Manager может скачать драйвер при первом
запуске; для заранее подготовленного драйвера есть `--driver /absolute/path`.
Браузер и Selenium Manager могут выполнять служебные сетевые запросы;
browser smoke не является offline-проверкой или сетевой изоляцией.

## Новый runner

`Source/bots/main.py` теперь использует Playwright и конечный сценарий.
Старый бесконечный main архивирован в `Source/legacy/main.py`.
Команды и описание нового runner находятся в корневом README.md.
Проверка нового кода с настоящим браузером на локальном fixture:

```sh
.venv/bin/python -m pytest sandbox/test_runner.py -q
```
