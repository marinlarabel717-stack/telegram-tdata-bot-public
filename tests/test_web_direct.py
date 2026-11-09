import ast
import asyncio
import logging
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import web_direct

URL = 'https://web.telegram.org/a/#tgWebAuthToken=test&tgWebAuthUserId=123'

class Accepted:
    def __init__(self, url): self.url = url

class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_stops_without_second_url_and_reports_wait(self):
        for wait in (None, 123):
            class FloodError(Exception):
                code = 420
            error = FloodError('private auth token must not leak')
            error.seconds = wait
            calls = []
            class Client:
                connect = AsyncMock()
                disconnect = AsyncMock()
                is_user_authorized = AsyncMock(return_value=True)
                async def __call__(self, request):
                    calls.append(request)
                    raise error
            client = Client()
            with patch.dict(sys.modules, {'telethon': self.library()}):
                result = await web_direct.generate_link('x', 'account', client_factory=lambda *a, **kw:client, session_loader=lambda p:p)
            self.assertEqual(len(calls), 1)
            self.assertIn('限流', result['error'])
            self.assertNotIn('private auth token', result['error'])
            if wait:
                self.assertIn('123', result['error'])
            client.disconnect.assert_awaited_once()

    async def test_no_token_reports_actual_result_without_leaking_url(self):
        class Default: pass
        class Request: pass
        module = self.library()
        module.types.UrlAuthResultDefault = Default
        module.types.UrlAuthResultRequest = Request
        for response, expected in ((Default(), 'UrlAuthResultDefault'), (Request(), 'UrlAuthResultRequest'), (Accepted('https://evil.test/private'), '缺少有效')):
            class Client:
                connect = AsyncMock()
                disconnect = AsyncMock()
                is_user_authorized = AsyncMock(return_value=True)
                async def __call__(self, request): return response
            with patch.dict(sys.modules, {'telethon':module}):
                result = await web_direct.generate_link('x', 'account', client_factory=lambda *a, **kw:Client(), session_loader=lambda p:p)
            self.assertIn(expected, result['error'])
            self.assertNotIn('evil.test', result['error'])

    def library(self):
        module = ModuleType('telethon')
        module.TelegramClient = MagicMock()
        module.functions = SimpleNamespace(messages=SimpleNamespace(RequestUrlAuthRequest=lambda **kw: kw))
        module.types = SimpleNamespace(UrlAuthResultAccepted=Accepted, InputPeerEmpty=lambda: None)
        return module

    async def test_success_and_disconnect(self):
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.__call__ = AsyncMock(return_value=Accepted(URL))
        # Special method lookup must be defined on the class.
        class Client:
            connect = client.connect
            disconnect = client.disconnect
            is_user_authorized = client.is_user_authorized
            async def __call__(self, request): return Accepted(URL)
        factory = MagicMock(return_value=Client())
        with patch.dict(sys.modules, {'telethon': self.library()}):
            result = await web_direct.generate_link('test.session', 'a.session', client_factory=factory, session_loader=lambda path:path)
        self.assertEqual(result['url'], URL)
        client.disconnect.assert_awaited_once()
        self.assertEqual(factory.call_args.args[1], 2040)

    async def test_unauthorized_cleans_up(self):
        client = MagicMock(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False))
        with patch.dict(sys.modules, {'telethon': self.library()}):
            result = await web_direct.generate_link('test.session', 'a', client_factory=lambda *a, **kw: client, session_loader=lambda path:path)
        self.assertNotIn('url', result)
        client.disconnect.assert_awaited_once()

    async def test_failure_does_not_expose_exception_secret(self):
        client = MagicMock(connect=AsyncMock(side_effect=RuntimeError('secret token')), disconnect=AsyncMock())
        with patch.dict(sys.modules, {'telethon': self.library()}):
            result = await web_direct.generate_link('test.session', 'a', client_factory=lambda *a, **kw: client, session_loader=lambda path:path)
        self.assertIn('RuntimeError', result['error'])
        self.assertNotIn('secret', str(result))

    async def test_request_legacy_constructor(self):
        constructors = MagicMock(side_effect=[TypeError(), 'legacy'])
        functions = SimpleNamespace(messages=SimpleNamespace(RequestUrlAuthRequest=constructors))
        client = AsyncMock(return_value=Accepted(URL))
        await web_direct.request_token(client, 'https://web.telegram.org/a/', functions, self.library().types)
        self.assertEqual(constructors.call_count, 2)
        self.assertEqual(constructors.call_args.kwargs['msg_id'], 0)

    def test_official_urls_only(self):
        self.assertTrue(web_direct.accepted_url(URL))
        for value in ['http://web.telegram.org/a/#tgWebAuthToken=x', 'https://evil.test/#tgWebAuthToken=x', 'https://web.telegram.org.evil.test/a/#tgWebAuthToken=x', 'https://web.telegram.org/a/', URL+'\n']:
            self.assertFalse(web_direct.accepted_url(value))

    def test_extract_and_path_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/'batch.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('a/account.session', b'a')
                output.writestr('b/account.session', b'b')
                output.writestr('ignore.json', '{}')
            found = web_direct.collect_sessions(archive, root/'out')
            self.assertEqual(len(found), 2)
            self.assertEqual([p.read_bytes() for p, _ in found], [b'a', b'b'])
            for bad in ['../escape.session','C:/escape.session','/absolute.session']:
                with zipfile.ZipFile(archive, 'w') as output: output.writestr(bad, b'x')
                with self.assertRaises(ValueError): web_direct.collect_sessions(archive, root/'bad')

    def test_limits_and_task_isolation(self):
        self.assertTrue(web_direct.claim(1))
        self.assertFalse(web_direct.claim(1))
        self.assertTrue(web_direct.claim(2))
        web_direct.release(1); web_direct.release(2)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)/'batch.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                for index in range(201): output.writestr(str(index)+'.session', b'x')
            with self.assertRaises(ValueError): web_direct.collect_sessions(archive, Path(directory)/'out')

    async def test_bot_returns_private_txt_and_cleans_temp(self):
        await self._check_one_txt(1)

    async def test_large_uploads_return_one_txt_including_failures(self):
        for count in (6, 100, 200):
            with self.subTest(accounts=count):
                await self._check_one_txt(count)

    async def _check_one_txt(self, count):
        # Load the touched method only: avoid booting the production bot/database.
        tree = ast.parse(Path('tdata.py').read_text(encoding='utf-8'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name=='EnhancedBot')
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name=='process_web_direct')
        module = ast.Module(body=[method], type_ignores=[])
        namespace = dict(web_direct=web_direct, tempfile=tempfile, Path=Path, BytesIO=BytesIO, asyncio=asyncio, logger=logging.getLogger('test'))
        exec(compile(module, 'tdata.py', 'exec'), namespace)
        downloads=[]
        def download(target):
            downloads.append(target); Path(target).write_bytes(b'fake session')
        document = SimpleNamespace(file_name='batch.zip', get_file=lambda:SimpleNamespace(download=download))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        delivered=[]
        def send(**kw):
            delivered.append((kw['chat_id'], kw['filename'], kw['document'].getvalue(), kw['caption']))
        context = SimpleNamespace(bot=SimpleNamespace(send_document=send))
        bot = SimpleNamespace(safe_send_message=MagicMock(return_value=object()), safe_edit_message_text=MagicMock(), db=object(), proxy_manager=SimpleNamespace(is_proxy_mode_active=lambda db:False))
        async def generate(path, label, proxy=None):
            self.assertEqual(len(delivered), 0)  # no early/partial documents
            if label == 'account-3.session':
                return {'name':label, 'error':'Unauthorized'}
            return {'name':label, 'url':URL}
        found = [(Path('unused'), f'account-{index}.session') for index in range(count)]
        with patch.object(web_direct, 'collect_sessions', return_value=found), patch.object(web_direct, 'generate_link', side_effect=generate):
            await namespace['process_web_direct'](bot, update, context, document)
        self.assertEqual(len(delivered), 1)
        body = delivered[0][2].decode('utf-8-sig')
        self.assertEqual(body.count('Account: '), count)
        for _, label in found:
            self.assertIn(label, body)
        if count > 3:
            self.assertIn('Unauthorized', body)
        self.assertEqual(delivered[0][0], 7)
        self.assertTrue(delivered[0][1].endswith('.txt'))
        self.assertIn(URL, delivered[0][2].decode('utf-8-sig'))
        self.assertFalse(Path(downloads[0]).exists())
        self.assertIn('浏览器', delivered[0][3])

class SessionSchemaTests(unittest.TestCase):
    def setUp(self):
        try:
            import telethon
        except ImportError:
            self.skipTest("Telethon is required for real offline session compatibility tests")

    def test_new_and_old_schema_read_without_mutation(self):
        import sqlite3
        for extra in (False, True):
            with self.subTest(extra_field=extra), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/"account.session"
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE version (version INTEGER)")
                connection.execute("INSERT INTO version VALUES(999)")
                columns = "dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB, takeout_id INTEGER"
                if extra:
                    columns += ", tmp_auth_key BLOB"
                connection.execute("CREATE TABLE sessions(" + columns + ")")
                values = (2, "149.154.167.51", 443, bytes(range(256)), None) + ((None,) if extra else ())
                connection.execute("INSERT INTO sessions VALUES(" + ",".join("?" for _ in values) + ")", values)
                connection.commit(); connection.close()
                before = path.read_bytes()
                memory = web_direct.load_memory_session(path)
                self.assertEqual(memory.auth_key.key, bytes(range(256)))
                self.assertEqual(memory.dc_id, 2)
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_file_is_actionable_without_token_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"bad.session"
            path.write_bytes(b"invalid sqlite data")
            with self.assertRaises(web_direct.WebSessionError) as result:
                web_direct.load_memory_session(path)
            self.assertIn("session", str(result.exception))
            self.assertNotIn("invalid sqlite data", str(result.exception))

    def test_pyrogram_schema_is_identified(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"pyrogram.session"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sessions(dc_id INTEGER, api_id INTEGER, auth_key BLOB)")
            connection.commit(); connection.close()
            with self.assertRaises(web_direct.WebSessionError) as result:
                web_direct.load_memory_session(path)
            self.assertIn("Pyrogram", str(result.exception))

if __name__=='__main__': unittest.main()
