"""Private, disposable shared SQLite snapshots for the public demo only."""
import base64
import json
import sqlite3
import time
import uuid
import zlib
from contextlib import closing

import httpx


class DemoStoreUnavailable(Exception):
    pass


def snapshot(paths):
    result = {}
    for name, path in paths.items():
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(':memory:')) as target:
            source.backup(target)
            result[name] = base64.b64encode(zlib.compress(target.serialize())).decode()
    return result


def restore(paths, databases):
    for name, path in paths.items():
        with closing(sqlite3.connect(':memory:')) as source, closing(sqlite3.connect(path)) as target:
            data = bytearray(zlib.decompress(base64.b64decode(databases[name])))
            # A backup already contains committed WAL pages. In-memory SQLite cannot
            # reopen a WAL sidecar; normalize its read/write format to rollback mode.
            data[18:20] = b'\x01\x01'
            source.deserialize(bytes(data))
            source.backup(target)


class BlobStore:
    def __init__(self, token, paths, transport=None):
        self.paths = paths
        self.seed = snapshot(paths)
        # Compression converts the CDN ETag into a weak validator, which cannot
        # be used in a conditional write. Fetch the identity representation.
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
        sid = token.split('_')[3].lower()
        self.url = f'https://{sid}.private.blob.vercel-storage.com/dataforge-registros-v1.json'
        self.client = httpx.AsyncClient(timeout=15, transport=transport)

    async def read(self):
        r = await self.client.get(self.url, params={'cache': '0'}, headers=self.headers)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        return r.json(), r.headers['etag']

    async def put(self, document, etag=None):
        headers = dict(self.headers, **{'x-api-version': '11', 'x-vercel-blob-access': 'private',
            'x-add-random-suffix': '0', 'x-allow-overwrite': '1' if etag else '0',
            'x-content-type': 'application/json', 'x-cache-control-max-age': '60'})
        if etag:
            headers['x-if-match'] = etag
        r = await self.client.put('https://vercel.com/api/blob', params={'pathname': 'dataforge-registros-v1.json'},
                                 headers=headers, content=json.dumps(document).encode())
        if r.status_code in (409, 412):
            return False
        # Blob's create-only conflict may be reported as 400.
        if not etag and r.status_code == 400 and 'already exists' in r.text.lower():
            return False
        r.raise_for_status()
        return True

    async def load(self, write=False):
        import asyncio
        deadline = time.monotonic() + 18
        owner = uuid.uuid4().hex
        while time.monotonic() < deadline:
            document, etag = await self.read()
            if document is None:
                await self.put({'databases': self.seed, 'owner': '', 'lease_until': 0, 'version': 0})
                continue
            if not write:
                restore(self.paths, document['databases'])
                return None
            if document.get('owner') and document['lease_until'] > time.time():
                await asyncio.sleep(.3)
                continue
            leased = dict(document, owner=owner, lease_until=time.time() + 90)
            if not await self.put(leased, etag):
                continue
            confirmed, lease_etag = await self.read()
            if confirmed.get('owner') != owner:
                raise DemoStoreUnavailable('Lease lost')
            try:
                restore(self.paths, document['databases'])
            except Exception:
                await self.put(dict(confirmed, owner='', lease_until=0), lease_etag)
                raise
            return (confirmed, lease_etag)
        raise DemoStoreUnavailable('Store busy')

    async def finish(self, lease, commit=True):
        if not lease:
            return
        document, etag = lease
        document = dict(document, owner='', lease_until=0)
        if commit:
            document['databases'] = snapshot(self.paths)
            document['version'] += 1
        if not await self.put(document, etag):
            raise DemoStoreUnavailable('Concurrent update rejected')
