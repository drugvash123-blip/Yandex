"""Convert a line-separated proxy file into a JSON array of configurations."""
import argparse
import ipaddress
import json
import os
from pathlib import Path


def convert(value, scheme='socks5'):
    address, separator, credentials = value.strip().partition('@')
    username, colon, password = credentials.partition(':')
    if not separator or not colon or not username or not password:
        raise ValueError('Ожидается ip:port@login:password')
    host, separator, port = address.rpartition(':')
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Порт должен быть числом от 1 до 65535')
    try:
        ip = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        raise ValueError('Укажите корректный IP-адрес') from None
    if scheme not in ('socks5', 'http', 'https'):
        raise ValueError('Допустимы socks5, http, https')
    host = f'[{ip}]' if ip.version == 6 else str(ip)
    return {'server': f'{scheme}://{host}:{int(port)}', 'username': username, 'password': password}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Входной файл: одна строка ip:port@login:password на прокси')
    parser.add_argument('output', type=Path, help='Выходной JSON-файл (массив конфигураций)')
    parser.add_argument('--scheme', choices=('socks5', 'http', 'https'), default='socks5')
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('Входной и выходной файлы должны отличаться')
    try:
        lines = args.input.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        parser.error('Не удалось прочитать входной файл в UTF-8')
    configs = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            configs.append(convert(line, args.scheme))
        except ValueError as error:
            parser.error(f'Строка {number}: {error}. Выходной файл не изменён')
    if not configs:
        parser.error('Входной файл не содержит прокси')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(configs, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    if os.name != 'nt':
        args.output.chmod(0o600)
    print(f'Сохранено прокси: {len(configs)}. Файл: {args.output}')


if __name__ == '__main__':
    main()
