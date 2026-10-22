"""Authorize review-selected image checks without checking out PR code."""
import argparse
import hashlib
import json
from pathlib import Path
import re

import releases


def select(event, pull):
    comment = event.get('comment') or {}
    if (event.get('action') != 'created' or 'pull_request' not in event.get('issue', {})
            or comment.get('user', {}).get('type') == 'Bot'):
        return None
    commands, fenced = [], False
    for line in comment.get('body', '').splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        elif not fenced and re.match(r'^/ci(?:\s|$)', line):
            commands.append(line.strip())
    if not commands:
        return None
    if comment.get('author_association') not in ('OWNER', 'MEMBER', 'COLLABORATOR'):
        raise ValueError('Only repository owners, members and collaborators may request image checks')
    if len(commands) != 1:
        raise ValueError('Use one CI command per comment')
    match = re.fullmatch(r'/ci run image-(quick|full)(?: on (all|[0-9]+\.[0-9]+\.[0-9]+))?', commands[0])
    if not match:
        raise ValueError('Use /ci run image-quick or image-full [on <release|all>]')
    repository = event.get('repository', {}).get('full_name')
    if (pull.get('state') != 'open' or pull.get('number') != event['issue'].get('number')
            or (pull.get('head', {}).get('repo') or {}).get('full_name') != repository
            or (pull.get('base', {}).get('repo') or {}).get('full_name') != repository):
        raise ValueError('Image checks require an open pull request in this repository; forks are refused')
    sha = pull.get('head', {}).get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('The pull request head must be a complete commit SHA')
    scope, requested = match.groups()
    selected = list(releases.RELEASES) if requested == 'all' else [requested or releases.DEFAULT]
    if any(value not in releases.RELEASES for value in selected):
        raise ValueError('Unsupported Ceph release')
    variants = ('official', 'debian', 'ubuntu') if scope == 'full' else ('official',)
    architectures = ('amd64', 'arm64') if scope == 'full' else ('amd64',)
    cells = [{'release': release, 'variant': variant, 'architecture': arch, 'scope': scope,
              'runner': 'ubuntu-24.04-arm' if arch == 'arm64' else 'ubuntu-24.04'}
             for release in selected for variant in variants for arch in architectures]
    context = 'Image checks / ' + str(comment['id'])
    key = hashlib.sha256(json.dumps([sha, cells], sort_keys=True).encode()).hexdigest()[:24]
    return {'sha': sha, 'matrix': {'include': cells}, 'context': context, 'key': key}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', type=Path, required=True)
    parser.add_argument('--pull', type=Path, required=True)
    parser.add_argument('--github-output', type=Path, required=True)
    args = parser.parse_args()
    result = select(json.loads(args.event.read_text()), json.loads(args.pull.read_text()))
    with args.github_output.open('a') as output:
        output.write('accepted='+('true' if result else 'false')+'\n')
        for key, value in (result or {}).items():
            output.write(key+'='+(json.dumps(value, separators=(',', ':')) if key == 'matrix' else value)+'\n')


if __name__ == '__main__':
    main()
