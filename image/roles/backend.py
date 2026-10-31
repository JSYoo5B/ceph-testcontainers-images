"""Resolve installed package closures offline for the roles assembler."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

def run(args):
    return subprocess.check_output(args, text=True).splitlines()

def load(kind, analyze):
    if kind == "rpm":
        return analyze.installed_inventory(), analyze.closure, analyze.BASE + ("procps-ng",)
    inventory = {}
    if kind == "deb":
        for line in run(["dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${Version}\t${Depends}\t${Pre-Depends}\t${Provides}\t${Essential}\t${source:Package}\t${source:Version}\n"]):
            name, arch, version, deps, pre, provides, essential, source, source_version = line.split("\t")
            listing = next((p for p in [Path('/var/lib/dpkg/info')/(name+'.list'), Path('/var/lib/dpkg/info')/(name+':'+arch+'.list')] if p.exists()), None)
            if listing is None:
                continue
            files = listing.read_text().splitlines()
            inventory[name] = dict(files=files, licenses={p for p in files if p.endswith('/copyright')}, version=version,
                                   source_rpm=source+'='+source_version, vendor='Debian package source',
                                   depends=deps+','+pre, provides=provides, essential=essential=='yes')
        aliases = {name: name for name in inventory}
        for name, pkg in inventory.items():
            for item in pkg['provides'].split(','):
                key = item.strip().split(' ')[0]
                if key:
                    aliases.setdefault(key, name)
        def closure(names):
            selected, todo = set(), list(names) + [n for n,p in inventory.items() if p['essential']]
            while todo:
                name = todo.pop()
                if name in selected:
                    continue
                if name not in inventory:
                    raise RuntimeError('Missing installed package '+name)
                selected.add(name)
                for dep in inventory[name]['depends'].split(','):
                    if not dep.strip():
                        continue
                    choices = [re.split(r'\s|:', p.strip())[0] for p in dep.split('|')]
                    match = next((aliases[p] for p in choices if p in aliases), None)
                    if match is None:
                        raise RuntimeError('Unresolved installed dependency '+name+': '+dep)
                    todo.append(match)
            return selected
        return inventory, closure, ('bash','coreutils','hostname','mawk','ca-certificates','base-files','base-passwd','procps')
    if kind != "apk":
        raise RuntimeError('Unknown package backend '+kind)
    for record in Path('/lib/apk/db/installed').read_text().strip().split('\n\n'):
        fields, files, directory = {}, [], ''
        for line in record.splitlines():
            key, sep, value = line.partition(':')
            if not sep:
                continue
            fields[key] = value
            if key == 'F':
                directory = value
                files.append('/'+value)
            elif key == 'R':
                files.append('/'+directory+'/'+value)
        if 'P' in fields:
            inventory[fields['P']] = dict(files=files, licenses={p for p in files if '/licenses/' in p}, version=fields['V'],
                source_rpm=fields.get('o',fields['P'])+'@'+fields.get('c','unknown'), vendor='Alpine aports; SPDX '+fields.get('L','unknown'))
    original = Path('/etc/apk/world').read_text().splitlines()
    def closure(names):
        # APK's own installed solver handles soname providers and versioned
        # virtual dependencies; never infer those from package names.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'lib/apk/db').mkdir(parents=True)
            (root/'etc/apk').mkdir(parents=True)
            shutil.copyfile('/lib/apk/db/installed',root/'lib/apk/db/installed')
            (root/'etc/apk/world').write_text('\n'.join(original)+'\n')
            command = ['apk','--root',folder,'--no-scripts','--no-network']
            run(command+['add',*names])
            remove = [re.split(r'[<>=~]',p)[0] for p in original if re.split(r'[<>=~]',p)[0] not in names]
            if remove:
                run(command+['del',*remove])
            return set(re.findall(r'^P:(.+)$',(root/'lib/apk/db/installed').read_text(),re.M))
    return inventory, closure, ('ca-certificates','coreutils','procps')

def roots(kind):
    if kind == 'apk':
        return {
            # Alpine splits ceph-bluestore-tool and the other offline OSD tools into
            # ceph19-osd-tools; the role filter keeps only ceph-bluestore-tool.
            'essential': ('ceph19-mon','ceph19-mgr','ceph19-osd-daemon','ceph19-osd-tools','ceph19-common','py3-setuptools'),
            'client': ('ceph19-common','ceph19-base','py3-cephfs19','libradosstriper19','cryptsetup'),
            'cephfs': ('ceph19-mds','cephfs19-mirror','ceph19-common','py3-cephfs19'),
            'rbd-aux': ('ceph19','rbd-mirror19','ceph19-common','py3-rbd19'),
            'rgw': ('ceph19-radosgw','ceph19-common'),
        }
    common = ('ceph-common','python3-cephfs')
    result = {
        'essential': ('ceph-mon','ceph-mgr','ceph-osd','ceph-common'),
        'client': common+('ceph-base','libradosstriper1','cryptsetup' if kind=='rpm' else 'cryptsetup-bin'),
        'cephfs': common+('ceph-mds','cephfs-mirror'),
        'rbd-aux': ('ceph-common','rbd-mirror','ceph-immutable-object-cache'),
        'rgw': ('ceph-radosgw' if kind=='rpm' else 'radosgw','ceph-common'),
    }
    if kind == 'deb':
        result['essential'] += tuple(os.environ.get('CEPH_MGR_PACKAGES','').split())
    return result
