from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from tools.demo_store import BlobStore, DemoStoreUnavailable


class DemoStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = None
        self.version = 0
        self.fail_write = False
        def handler(req):
            etag = f'"{self.version}"'
            if req.method == 'GET':
                if self.data is None:
                    return httpx.Response(404)
                return httpx.Response(200,json=self.data,headers={'etag':etag})
            if self.fail_write:
                return httpx.Response(503)
            match = req.headers.get('x-if-match')
            if (match and match != etag) or (not match and self.data is not None):
                return httpx.Response(412)
            self.data = json.loads(req.content)
            self.version += 1
            return httpx.Response(200,json={'url':'private'})
        self.transport = httpx.MockTransport(handler)
        self.stores = []
        for instance in ('a','b'):
            p=Path(self.temp.name)/f'{instance}.db'
            with closing(sqlite3.connect(p)) as c, c:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY, title TEXT)')
            store=BlobStore('vercel_blob_rw_fake_secret',{'platform':p},self.transport)
            self.stores.append(store)
            self.addAsyncCleanup(store.client.aclose)

    async def test_separate_instances_share_records_and_keep_sequential_updates(self):
        a,b=self.stores
        lease=await a.load(write=True)
        with closing(sqlite3.connect(a.paths['platform'])) as c, c:
            c.execute("INSERT INTO requests(title) VALUES('Fictício A')")
        await a.finish(lease)
        lease=await b.load(write=True)
        with closing(sqlite3.connect(b.paths['platform'])) as c, c:
            self.assertEqual(c.execute('SELECT title FROM requests').fetchone()[0],'Fictício A')
            c.execute("INSERT INTO requests(title) VALUES('Fictício B')")
        await b.finish(lease)
        await a.load()
        with closing(sqlite3.connect(a.paths['platform'])) as c, c:
            self.assertEqual(c.execute('SELECT count(*) FROM requests').fetchone()[0],2)

    async def test_read_during_lease_and_failed_request_do_not_publish_uncommitted_rows(self):
        a,b=self.stores
        lease=await a.load(write=True)
        with closing(sqlite3.connect(a.paths['platform'])) as c, c:
            c.execute("INSERT INTO requests(title) VALUES('Não confirmado')")
        await b.load()
        with closing(sqlite3.connect(b.paths['platform'])) as c, c:
            self.assertEqual(c.execute('SELECT count(*) FROM requests').fetchone()[0],0)
        await a.finish(lease,commit=False)
        await a.load()
        with closing(sqlite3.connect(a.paths['platform'])) as c, c:
            self.assertEqual(c.execute('SELECT count(*) FROM requests').fetchone()[0],0)

    async def test_stale_writer_cannot_overwrite_new_lease(self):
        a,b=self.stores
        lease=await a.load(write=True)
        self.data['lease_until']=0
        new_lease=await b.load(write=True)
        with self.assertRaises(DemoStoreUnavailable):
            await a.finish(lease)
        await b.finish(new_lease)

    async def test_persistence_failure_is_propagated(self):
        lease=await self.stores[0].load(write=True)
        self.fail_write=True
        with self.assertRaises(httpx.HTTPStatusError):
            await self.stores[0].finish(lease)
