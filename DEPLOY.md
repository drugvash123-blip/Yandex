# Перенос и запуск

Нужны Python 3.11+ и установленный Google Chrome, а также рабочий стол
(окно Chrome должно быть доступно). Архив содержит исходники: отдельная
компиляция Python не требуется. Старый Selenium-worker не используется.

## Установка

Распакуйте архив и откройте терминал в папке проекта.

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.runtime.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.runtime.txt
```

В Windows в дальнейших командах заменяйте `python` на
`.venv\Scripts\python.exe`, если окружение не активировано.
ChromeDriver и `playwright install` для установленного Google Chrome не нужны.
Версии зависимостей зафиксированы по проверенной среде macOS/Python 3.11;
на другой ОС живой прогон пока не проверен.

## Локальная конфигурация

Создайте папку `Data` и файлы (они не входят в архив или Git):

- `Data/service-key.txt`: ключ prstar, одна строка.
- `Data/key.txt`: ключ CapGuru, одна строка.
- `Data/proxy.json`: параметры своего прокси:

```json
{
  "server": "socks5://PROXY_HOST:PROXY_PORT",
  "username": "PROXY_LOGIN",
  "password": "PROXY_PASSWORD"
}
```

Настоящие значения перенесите отдельно. Профиль Chrome создастся автоматически.
Можно перенести сохранённый `Data/browser-profiles/ab-webshare-a` с закрытым
браузером на совместимую среду, но перенос cookies между ОС не гарантируется.
По умолчанию на новом компьютере начните с нового профиля.

## Один полный цикл с заданиями prstar

Следующая команда подходит для обеих ОС: выполните её одной строкой.

```sh
python Source/bots/main.py --fetch-tasks --wait-for-tasks 31536000 --task-poll-interval 1 --limit-tasks 100 --service-key-file Data/service-key.txt --upload-stats --client-id worker-transfer-01 --proxy-config Data/proxy.json --profile Data/browser-profiles/worker-transfer-01 --headed --captcha-key-file Data/key.txt --captcha-max-tasks 0 --captcha-kinds both --captcha-wait 900 --deadline 1800 --max-pages 5 --action-pause 15 --visit-seconds 100 --output sandbox/artifacts/worker-transfer-01
```

Замените `worker-transfer-01` на имя своего запуска (для новых логов используйте
новую папку output). Программа ждёт первый непустой пакет с паузой в секунду
после каждого ответа. Затем выполняет до 100 заданий из пакета, отправляет
допустимые результаты и завершается. Это один цикл, а не бесконечный worker.
Ожидание заданий ограничено годом; Ctrl+C останавливает процесс.

Текст и силуэты обрабатываются через CapGuru. `--captcha-max-tasks 0` снимает
лимит числа платных задач; каждая новая задача может оплачиваться. После явного
отказа распознавания допускается одно обновление задания и повтор. Неизвестный
результат создания задачи после сетевой ошибки не повторяется. Неподдерживаемая
CAPTCHA или другая ошибка оставляет до 15 минут на ручное прохождение.
Slider сохранён в legacy-коде; современный runner автоматически его не решает.
Общий тайм-аут каждого поискового задания — 30 минут, включая ожидания.
На найденном сайте программа ждёт 100 секунд перед завершением задания.

## Результаты

В папке output находятся `runner.log`, `received-tasks.json`, `results.jsonl`,
`captcha-service.json`, квитанция `submission-*.json` и подпапки с диагностикой.
Логи и снимки не отправляйте в публичный Git: они могут содержать данные сессии.
HTTP 200 в квитанции подтверждает HTTP-ответ; запись проверяется в таблице prstar.
Код выхода 0 — задания завершены; 1 — неполный поиск; 2 — ошибка пакета/ожидания.

## Сборка архива

```sh
python sandbox/build_release.py
```

Архив и его SHA-256 появятся в `dist`. Ключи, профили, логи, виртуальное
окружение и старый бинарный ChromeDriver в сборку не включаются.
