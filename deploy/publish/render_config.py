#!/usr/bin/env python3
"""Render the bounded nginx template without evaluating shell or template code."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit


def parse_environment(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        name, separator, value = stripped.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', name):
            raise ValueError('Use plain NAME=value lines in the environment file.')
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'Unclosed quoted value: {name}')
            value = value[1:-1]
        values[name] = value
    return values


def hostname(value: str) -> str:
    normalized = value.lower().rstrip('.')
    if not value.isascii() or len(normalized) > 253 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in normalized.split('.')
    ):
        raise ValueError('Use an ASCII DNS hostname without a wildcard, port or path.')
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise ValueError('Production content isolation requires DNS names, not an IP address.')


def render(values: dict[str, str], template: str) -> str:
    base_url = values.get('PUBLISH_BASE_URL', '')
    origin = urlsplit(base_url)
    if (
        origin.scheme != 'https'
        or not origin.hostname
        or origin.hostname.endswith('.')
        or origin.username is not None
        or origin.password is not None
        or origin.path
        or origin.netloc.endswith(':')
        or origin.port not in (None, 443)
        or not base_url.isascii()
        or any(ord(character) < 33 or character in '?#\\\x7f' for character in base_url)
    ):
        raise ValueError(
            'PUBLISH_BASE_URL must be an HTTPS origin with an ASCII DNS hostname '
            'on port 443 without a path.'
        )
    control = hostname(origin.hostname)
    content = hostname(values.get('PUBLISH_CONTENT_DOMAIN', ''))
    if content != control:
        raise ValueError('PUBLISH_CONTENT_DOMAIN must equal the control hostname.')
    policy_path = Path(__file__).resolve().parents[2] / 'src/minder_publish/defaults.json'
    if not policy_path.is_file():
        policy_path = files('minder_publish').joinpath('defaults.json')
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    ceiling = (
        policy['ceilings']['upload_payload_bytes']
        + policy['limits']['multipart_overhead_bytes']
    )
    maximum = int(values.get('PUBLISH_PROXY_MAX_BODY_BYTES', ceiling))
    if not 1 <= maximum <= 17 * 1024**3:
        raise ValueError('Proxy body ceiling must be between 1 byte and 17 GiB.')
    for key, value in {'CONTROL_HOST': control, 'CONTENT_DOMAIN': content, 'MAX_BODY_BYTES': str(maximum)}.items():
        template = template.replace(f'@@{key}@@', value)
    if '@@' in template:
        raise ValueError('Unknown nginx template placeholder.')
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='.env', help='Read this file or - for stdin.')
    parser.add_argument('--output', required=True, help='Write to this file or - for stdout.')
    args = parser.parse_args()
    environment = sys.stdin.read() if args.env_file == '-' else Path(args.env_file).read_text(encoding='utf-8')
    source = Path(__file__).with_name('nginx.conf.template').read_text(encoding='utf-8')
    content = render(parse_environment(environment), source)
    if args.output == '-':
        sys.stdout.write(content)
        return
    output = Path(args.output)
    output.write_text(content, encoding='utf-8')
    print(f'Rendered {output}')


if __name__ == '__main__':
    main()
