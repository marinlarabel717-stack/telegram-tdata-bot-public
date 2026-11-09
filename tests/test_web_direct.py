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
            result = await web_direct.generate_link('test.session', 'a.session', client_factory=factory)
        self.assertEqual(result['url'], URL)
        client.disconnect.assert_awaited_once()
        self.assertEqual(factory.call_args.args[1], 2040)

    async def test_unauthorized_cleans_up(self):
        client = MagicMock(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False))
        with patch.dict(sys.modules, {'telethon': self.library()}):
            result = await web_direct.generate_link('test.session', 'a', client_factory=lambda *a, **kw: client)
        self.assertNotIn('url', result)
        client.disconnect.assert_awaited_once()

    async def test_failure_does_not_expose_exception_secret(self):
        client = MagicMock(connect=AsyncMock(side_effect=RuntimeError('secret token')), disconnect=AsyncMock())
        with patch.dict(sys.modules, {'telethon': self.library()}):
            result = await web_direct.generate_link('test.session', 'a', client_factory=lambda *a, **kw: client)
        self.assertEqual(result['error'], 'RuntimeError')
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
        document = SimpleNamespace(file_name='123.session', get_file=lambda:SimpleNamespace(download=download))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
        delivered=[]
        def send(**kw):
            delivered.append((kw['chat_id'], kw['filename'], kw['document'].getvalue(), kw['caption']))
        context = SimpleNamespace(bot=SimpleNamespace(send_document=send))
        bot = SimpleNamespace(safe_send_message=MagicMock(return_value=object()), safe_edit_message_text=MagicMock(), db=object(), proxy_manager=SimpleNamespace(is_proxy_mode_active=lambda db:False))
        with patch.object(web_direct, 'generate_link', AsyncMock(return_value={'name':'123.session', 'url':URL})):
            await namespace['process_web_direct'](bot, update, context, document)
        self.assertEqual(delivered[0][0], 7)
        self.assertTrue(delivered[0][1].endswith('.txt'))
        self.assertIn(URL, delivered[0][2].decode('utf-8-sig'))
        self.assertFalse(Path(downloads[0]).exists())
        self.assertIn('浏览器', delivered[0][3])

if __name__=='__main__': unittest.main()
