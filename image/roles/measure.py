#!/usr/bin/env python3
"""Measure actual saved layer unions; never add Docker .Size across roles."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

import contract

def inspect(reference):
    return json.loads(subprocess.check_output(['docker','image','inspect',reference],text=True))[0]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', action='append',required=True,metavar='ROLE=REF')
    parser.add_argument('--baseline',help='Existing all image, compared by its actual layer union')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must not exist')
    refs = {}
    for item in args.image:
        role,sep,ref = item.partition('=')
        if not sep or role not in contract.ROLES+('all',) or role in refs or not ref:
            parser.error('expected distinct ROLE=REF for roles-v2')
        refs[role] = ref
    if args.baseline:
        refs['baseline'] = args.baseline
    images = {role:inspect(ref) for role,ref in refs.items()}
    if len({(i['Os'],i['Architecture']) for i in images.values()}) != 1:
        parser.error('all images must use one platform')
    result = {'scope':'actual Docker-save layer payload; excludes configs/manifests/protocol; not physical disk or network throughput',
              'platform':next(iter(images.values()))['Architecture'],'images':{},'layers':{},'deployments':{}}
    with tempfile.TemporaryDirectory(prefix='ceph-role-layers-') as folder:
        archive = Path(folder)/'images.tar'
        subprocess.run(['docker','image','save','--output',str(archive),*[i['Id'] for i in images.values()]],check=True)
        with tarfile.open(archive) as saved:
            records = json.load(saved.extractfile('manifest.json'))
            configurations = {}
            for record in records:
                data = saved.extractfile(record['Config']).read()
                configurations['sha256:'+hashlib.sha256(data).hexdigest()] = record
                for path in record['Layers']:
                    member = saved.getmember(path)
                    with saved.extractfile(member) as stream:
                        digest = hashlib.sha256()
                        while True:
                            chunk = stream.read(1<<20)
                            if not chunk: break
                            digest.update(chunk)
                    identity = 'sha256:'+digest.hexdigest()
                    if identity not in result['layers']:
                        with saved.extractfile(member) as stream:
                            compressed = stream.read(2) == b'\x1f\x8b'
                        with saved.extractfile(member) as stream:
                            content = gzip.GzipFile(fileobj=stream) if compressed else stream
                            diff,unpacked = hashlib.sha256(),0
                            while True:
                                chunk = content.read(1<<20)
                                if not chunk: break
                                diff.update(chunk);unpacked+=len(chunk)
                        result['layers'][identity] = dict(stored_bytes=member.size,encoding='gzip' if compressed else 'tar',
                                                          diff_id='sha256:'+diff.hexdigest(),uncompressed_tar_bytes=unpacked)
                    record.setdefault('identities',[]).append(identity)
            for role,observed in images.items():
                config = observed.get('Descriptor',{}).get('annotations',{}).get('config.digest',observed['Id'])
                record = configurations[config]
                identities = record['identities']
                assert [result['layers'][i]['diff_id'] for i in identities] == observed['RootFS']['Layers'],role
                result['images'][role] = dict(reference=refs[role],image_id=observed['Id'],docker_size_bytes=observed['Size'],layers=identities)
    scenarios = {'essential-client':('essential','client'),'rbd-aux':('essential','client','rbd-aux'),
                 'cephfs':('essential','client','cephfs'),'rgw':('essential','rgw'),
                 'complete':contract.ROLES,'all':('all',),'baseline':('baseline',)}
    for name,roles in scenarios.items():
        if not set(roles).issubset(result['images']): continue
        layers = set().union(*(set(result['images'][r]['layers']) for r in roles))
        result['deployments'][name] = dict(roles=roles,unique_layers=len(layers),
            stored_layer_union_bytes=sum(result['layers'][i]['stored_bytes'] for i in layers),
            encodings=sorted({result['layers'][i]['encoding'] for i in layers}))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result['deployments'],indent=2))

if __name__ == '__main__':
    main()
