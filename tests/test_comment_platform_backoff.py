"""Account-wide comment rejections must preserve messages and leave DMs independent."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('BILI_TEST_VENDOR',str(Path(__file__).resolve().parents[1]/'vendor'/'bilibili-mcp')))
import conversation as c
import private_messages as dm
import reply_publish as p
import reply_controls as controls
from bilibili_api import Credential,comment
from bilibili_api.exceptions import ResponseCodeException


class CommentPlatformBackoff(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.home=Path(folder.name)
        for name,value in [('STATE_FILE',self.home/'conversations.json'),('CONFIG_FILE',self.home/'config.json'),
                           ('OWNER_UID','900001'),('MIN_SEND_INTERVAL',0)]:
            setting=patch.object(c,name,value);setting.start();self.addCleanup(setting.stop)
        c.CONFIG_FILE.write_text(json.dumps({'replyToComments':True,'replyToPrivateMessages':True,'autoComment':True}),encoding='utf-8')
        self.cred=Credential(sessdata='fixture-session',bili_jct='fixture-csrf',buvid3='fixture-device',dedeuserid='900002')
        with c.locked_state() as state:
            for n in ['101','102']:
                state['pending'][n]={'rpid':n,'root':'100','aid':42,'bvid':'BV1aaaaaaaaa','uid':'900099',
                                       'name':'访客','text':'你好','time':int(n)}
            dm.section(state)['pending']['dm-fixture']={'message_id':'dm-fixture','uid':c.OWNER_UID,
                'name':'主人','text':'你好','time':1,'seqno':1,'msg_key':'1234'}
        self.sender=AsyncMock(return_value={'rpid_str':'90001'})
        mock=patch.object(c.comment,'send_comment',self.sender);mock.start();self.addCleanup(mock.stop)

    def state(self):return c.read_json(c.STATE_FILE,{})

    async def queue(self):
        with patch.object(c,'scan_notifications',AsyncMock(return_value=([],{},None))),\
             patch('public_inbox.backfill',AsyncMock(return_value=([],{'done':True},None))),\
             patch('public_inbox.resolve',AsyncMock(return_value=([],{},{},[]))):
            return await c.pending_replies(self.cred)

    async def test_quota_rejection_defers_account_preserves_targets_and_counts(self):
        self.sender.side_effect=ResponseCodeException(12014,'已达到当前评论发送上限')
        result=await c.send_guarded('BV1aaaaaaaaa','你好',101,100,self.cred)
        self.assertTrue(result['platform_limited']);self.assertTrue(result['stop_round'])
        state=self.state();self.assertEqual(len(state['pending']),2)
        self.assertEqual(state['pending']['101']['send_failures'],0)
        self.assertNotIn('blocked_reason',state['pending']['101'])
        self.assertEqual(c.day_counts(state)['replies'],0)
        queue=await self.queue();self.assertEqual(queue['queued'],0)
        self.assertTrue(queue['platform_backoff']['deferred'])
        self.assertEqual(len(dm.select(self.state())['pending']),1)
        again=await c.send_guarded('BV1aaaaaaaaa','第二条',102,100,self.cred)
        self.assertTrue(again['deferred']);self.sender.assert_awaited_once()

    async def test_expiry_recovers_and_success_is_not_duplicated(self):
        self.sender.side_effect=ResponseCodeException(12014,'评论发送上限')
        await c.send_guarded('BV1aaaaaaaaa','你好',101,100,self.cred)
        until=self.state()['public_reply_retry_after']
        self.sender.side_effect=None
        with patch.object(p.time,'time',return_value=until+1):
            result=await c.send_guarded('BV1aaaaaaaaa','你好',101,100,self.cred)
            self.assertTrue(result['success'])
            duplicate=await c.send_guarded('BV1aaaaaaaaa','你好',101,100,self.cred)
            self.assertFalse(duplicate['success'])
        self.assertEqual(self.sender.await_count,2)
        self.assertEqual(self.state()['public_comment_backoff']['failures'],0)

    async def test_removed_target_still_does_not_block_other_messages(self):
        self.sender.side_effect=ResponseCodeException(12022,'原评论不可用')
        with patch.object(c,'probe_reply_target',AsyncMock(return_value={'exists':False})):
            result=await c.send_guarded('BV1aaaaaaaaa','你好',101,100,self.cred)
        self.assertTrue(result['continue_queue']);self.assertFalse(result['platform_limited'])
        self.assertEqual([r['rpid'] for r in (await self.queue())['pending']],['102'])

    def test_repeated_windows_back_off_but_parallel_errors_do_not_multiply(self):
        state={};now=time.time()
        with patch.object(p.time,'time',return_value=now):
            p.record_comment_limit(state,12014,'上限');first=state['public_reply_retry_after']
            for _ in range(10):p.record_comment_limit(state,12014,'上限')
            self.assertEqual(state['public_reply_retry_after'],first)
            self.assertEqual(state['public_comment_backoff']['failures'],1)
            p.record_comment_success(state);self.assertTrue(p.comment_backoff(state))
        with patch.object(p.time,'time',return_value=first+1):
            p.record_comment_limit(state,12014,'上限')
            self.assertEqual(state['public_comment_backoff']['failures'],2)
            self.assertEqual(state['public_reply_retry_after'],first+1201)
        for code in [-412,-352]:self.assertTrue(p.account_limited(code))
        self.assertFalse(p.account_limited(12022,'目标已删除'))

    def test_migration_preserves_audit_and_independent_blocks(self):
        state=self.state();original={'send_failures':3,'retry_after':time.time()+3600,
            'blocked_reason':'同一目标连续被拒，保留记录等待核查，不阻塞其他来信',
            'last_send_error':{'code':12014,'message':'上限','at':time.time()}}
        state['pending']['101'].update(copy.deepcopy(original))
        state['pending']['102'].update(copy.deepcopy(original),blocked_reason='原评论已不可见',target_probe={'exists':False})
        p.recover_account_rejections(state)
        first=state['pending']['101'];self.assertNotIn('blocked_reason',first)
        self.assertEqual(first['last_send_error'],original['last_send_error'])
        self.assertEqual(first['account_backoff_migrated']['previous_send_failures'],3)
        self.assertEqual(state['pending']['102']['blocked_reason'],'原评论已不可见')
        once=copy.deepcopy(state);p.recover_account_rejections(state);self.assertEqual(state,once)

    async def test_native_guard_prevents_uploads_and_all_comment_destinations(self):
        with c.locked_state() as state:p.record_comment_limit(state,12014,'上限')
        with patch.object(p,'upload_image',AsyncMock()) as upload,patch.object(p,'Api') as api:
            for type_ in [comment.CommentResourceType.VIDEO,comment.CommentResourceType.DYNAMIC]:
                with self.assertRaises(p.CommentDeferred):
                    await p.send_comment_native('图片',42,type_,self.cred,root=100,parent=101,pic=object())
            upload.assert_not_awaited();api.assert_not_called()

    async def test_native_rejection_updates_shared_backoff_and_acknowledgement_waits(self):
        async def rejected():raise ResponseCodeException(12014,'已达到评论发送上限')
        api=SimpleNamespace(update_data=lambda **data:SimpleNamespace(result=rejected()))
        with patch.object(p,'Api',return_value=api):
            with self.assertRaises(ResponseCodeException):
                await p.send_comment_native('你好',42,comment.CommentResourceType.VIDEO,self.cred)
        with c.locked_state() as state:
            state['reply_control_events']={'event':{'acknowledgement':{'status':'waiting'}}}
        await controls.acknowledge(self.cred)
        self.assertEqual(self.state()['reply_control_events']['event']['acknowledgement']['status'],'waiting')


if __name__=='__main__':unittest.main(verbosity=2)
