import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from app_server import initialize, mutate, authenticate, CATEGORIES
from bot import Store

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'db.sqlite'
        old=Store(self.path)
        with old.db:
            old.put('owner',42)
            old.put('balance',90)
            old.db.execute("INSERT INTO tasks(title,difficulty,source) VALUES('old',3,'test')")
        old.db.close()
        initialize(self.path)
        self.db=sqlite3.connect(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def action(self,op,**fields):
        with self.db:
            mutate(self.db,dict(op=op,request_id=str(fields),**fields))

    def test_reset_once_owner_preserved(self):
        self.assertEqual(self.db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()[0],'42')
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='balance'").fetchone())
        self.action('create',title='Новое',category='bed')
        initialize(self.path)
        self.assertEqual(self.db.execute('SELECT count(*) FROM steps').fetchone()[0],1)

    def test_edit_and_limits_and_category(self):
        self.action('create',title='а'*40,category='bed')
        self.action('edit',id=1,title='Помыть зеркало',category='bath')
        self.assertEqual(self.db.execute('SELECT title,category FROM steps').fetchone(),('Помыть зеркало','bath'))
        for title,category in [('а'*41,'bed'),('  ','bed'),('Текст','nope')]:
            with self.assertRaises(ValueError):
                self.action('create',title=title,category=category)
        self.assertEqual(len(CATEGORIES),10)

    def test_complete_restore_clear_idempotent(self):
        self.action('create',title='Задача',category='bed')
        self.action('create',title='Задача',category='bed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM steps').fetchone()[0],1)
        self.action('complete',id=1)
        self.assertEqual(self.db.execute('SELECT done FROM steps').fetchone()[0],1)
        self.action('restore',id=1,request_id_unused='different')
        self.assertEqual(self.db.execute('SELECT done FROM steps').fetchone()[0],0)
        with self.db:
            mutate(self.db,{'op':'complete','id':1,'request_id':'again'})
        self.action('create',title='Открытая',category='bath')
        self.action('clear')
        self.assertEqual(self.db.execute('SELECT title,done FROM steps').fetchall(),[('Открытая',0)])

    def test_auth(self):
        token='test-token'
        data={'auth_date':'1000','user':json.dumps({'id':42})}
        secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
        data['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
        raw=urlencode(data)
        self.assertEqual(authenticate(raw,token,'42',now=1001),42)
        for bad_token,owner,now in [('bad','42',1001),(token,'43',1001),(token,'42',100000)]:
            with self.assertRaises(ValueError):authenticate(raw,bad_token,owner,now)

if __name__=='__main__':unittest.main()
