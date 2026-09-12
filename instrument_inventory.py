"""Bounded local discovery. Cache entries are not proof a plugin loads."""
import argparse
import json
from pathlib import Path


def inventory(resource_dir=None, content_roots=(), query='', limit=200):
    import reaperd
    if not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ValueError('limit must be 1..2000')
    resource = Path(resource_dir or reaperd.find_resource_dir())
    plugins, content = [], []
    needle = query.casefold()
    for pattern in ('reaper-vstplugins*.ini', 'reaper-clap*.ini', 'reaper-auplugins*.ini'):
        for cache in sorted(resource.glob(pattern)):
            for line in cache.read_text(encoding='utf-8', errors='replace').splitlines():
                if '=' not in line or needle not in line.casefold():
                    continue
                match = reaperd._FX_NAME_RE.search(line.replace('!!!VSTi', '').strip())
                if match:
                    plugins.append({'name': match.group().strip(), 'cache': cache.name,
                                    'instrument_flag': '!!!VSTi' in line,
                                    'verified_loaded': False})
    roots = list(content_roots)
    if not roots:
        serum = Path.home() / 'Documents/Xfer/Serum 2 Presets/Presets'
        if serum.is_dir():
            roots.append(str(serum))
    truncated = False
    for root in roots:
        directory = Path(root)
        if not directory.is_dir():
            raise ValueError(f'Content root is not a directory: {directory}')
        for path in directory.rglob('*'):
            if path.suffix.lower() not in ('.serumpreset', '.fxp', '.rfxchain', '.nki', '.sfz'):
                continue
            if needle not in str(path.relative_to(directory)).casefold():
                continue
            if len(content) == limit:
                truncated = True
                break
            content.append({'name': path.stem, 'path': str(path),
                            'loading': 'add_fx_chain' if path.suffix.lower() == '.rfxchain' else 'plugin_specific',
                            'dependencies_verified': False})
        if truncated:
            break
    return {'plugins': plugins[:limit], 'content': content,
            'truncated': truncated or len(plugins) > limit,
            'caveat': 'Cache and file discovery only. Loading, licensing, and sample dependencies require verification.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', default='')
    parser.add_argument('--root', action='append', default=[])
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(inventory(content_roots=args.root, query=args.query, limit=args.limit), indent=2))
