"""Viewing intent and evidence must not invalidate successful historical actions."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('BILI_TEST_VENDOR',str(Path(__file__).resolve().parents[1]/'vendor'/'bilibili-mcp')))
import conversation as c
import owner_tasks as o

A='BV1aaaaaaaaa'
B='BV1bbbbbbbbb'
C='BV1ccccccccc'


def analysis(bvid=A):
    return {'ok':True,'bvid':bvid,'source':'actual_sampled_video_and_audio',
            'analysed_at':time.time()-3600,'analysis':{
                'visual_observations':[{'timestamp':'00:01','observation':'蓝色角色在走路'}],
                'audio_observations':[{'timestamp':'00:01','observation':'背景音乐'}]}}


class VideoTaskEvidence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.cache=self.home/'video-cache'
        self.cache.mkdir()
        for name,value in [('STATE_FILE',self.home/'conversations.json'),('CONFIG_FILE',self.home/'config.json'),
                           ('OWNER_UID','900001'),('BOT_UID','900002')]:
            p=patch.object(c,name,value,create=True);p.start();self.addCleanup(p.stop)
        c.CONFIG_FILE.write_text('{"ownerTaskEnabled":true,"replyToComments":true}',encoding='utf-8')
        self.incoming={'uid':c.OWNER_UID,'rpid':'101','root':'99','bvid':B,'kind':'owner_mention',
                       'owner_agent_delegated':True,'mention_mode':'conversation','text':'从今天刷的视频里挑一个喜欢的，去点赞收藏'}
        self.task={'id':'fixture','token':'fixture-token','uid':c.OWNER_UID,'status':'running',
                   'channel':'comment','source_id':'101','scope':'full','domain':'bilibili',
                   'claimed_at':time.time(),'actions':{}}
        self.cred=SimpleNamespace(dedeuserid=c.BOT_UID)

    def store(self, data=None, suffix='valid'):
        file=self.cache/(A+'-0-'+suffix+'.json')
        file.write_text(json.dumps(data if data is not None else analysis()),encoding='utf-8')
        return file

    def start(self):
        with c.locked_state() as state:
            state['pending']['101']=copy.deepcopy(self.incoming)
            o.jobs(state)['fixture']=copy.deepcopy(self.task)

    def test_historical_selection_is_not_new_viewing(self):
        for verb in ['刷的','刷过的','看过的','看完的','看了的','观看的','观看过的','浏览过的']:
            for prefix in ['从今天','把你之前','在已经']:
                with self.subTest(verb=verb,prefix=prefix):
                    self.assertFalse(o.wants_video({'text':prefix+verb+'视频里选一个点赞收藏'}))
        self.assertFalse(o.wants_video({'text':'已经看过'+A+'，现在只需要收藏'}))

    def test_actual_viewing_including_mixed_history_remains_required(self):
        for text in ['去刷视频','观看'+A,'看完这个视频再收藏','从今天刷的视频里选一个重新看一遍再点赞',
                     '从看过的视频中选一个再看看','从看完的视频里选一个重看','把这个视频看一次']:
            with self.subTest(text=text):self.assertTrue(o.wants_video({'text':text}))
        self.assertTrue(o.wants_video({'kind':'owner_mention','text':'','bvid':A}))
        self.assertFalse(o.wants_video({'text':'不用看视频，只收藏'+A}))
        self.assertFalse(o.wants_video({'text':'去看这个动态 https://t.bilibili.com/123'}))

    def test_only_valid_actual_cache_is_reused_for_history(self):
        self.store()
        result=o.verify_video_completion(self.task,self.incoming,'我已经看过'+A+'，已点赞收藏。')
        self.assertTrue(result['ok'])
        self.assertEqual(result['targets'],[A])  # The source thread B is unrelated.
        self.assertEqual(result['proofs'][A]['origin'],'video-cache')
        self.assertFalse(o.video_evidence(self.task,A))  # Fresh viewing needs this task's call.
        self.assertFalse(o.video_evidence(self.task,B,allow_history=True))

    def test_bad_cache_records_are_ignored_independently(self):
        invalid=[]
        for changes in [{'ok':False},{'source':'subtitles'},{'bvid':B},{'analysis':{}},
                        {'analysed_at':time.time()+3600},{'analysed_at':True}]:
            invalid.append({**analysis(),**changes})
        for index,row in enumerate(invalid):self.store(row,str(index))
        self.store({'ok':True,'bvid':A,'source':'actual_video_and_audio'},'sampler')
        (self.cache/(A+'-corrupt.json')).write_text('{',encoding='utf-8')
        self.assertFalse(o.video_evidence(self.task,A,allow_history=True))
        silent=analysis();silent['analysis']['audio_observations']=[]
        self.store(silent)
        self.assertTrue(o.video_evidence(self.task,A,allow_history=True))

    def test_fresh_watch_cannot_use_old_cache_or_unrelated_proof(self):
        self.store()
        incoming={**self.incoming,'text':'去看'+A}
        self.assertFalse(o.verify_video_completion(self.task,incoming,'看完了')['ok'])
        task={**self.task,'media':{B:o._actual_media_proof(analysis(B),B)}}
        self.assertFalse(o.verify_video_completion(task,incoming,'已经看完')['ok'])
        task['media'][A]=o._actual_media_proof(analysis(),A)
        self.assertTrue(o.verify_video_completion(task,incoming,'已经看完')['ok'])
        self.assertFalse(o.verify_video_completion(task,{'text':'观看'+A+'和'+C},'看完了')['ok'])

    def test_failed_and_recovered_tool_invalidates_then_restores_evidence(self):
        self.start();self.store()
        o.record_tool('bili_video_understand',analysis(),task_id='fixture',token='fixture-token')
        o.record_tool('bili_video_understand',{'ok':False,'bvid':A},task_id='fixture',token='fixture-token')
        task=c.read_json(c.STATE_FILE,{})['owner_tasks']['fixture']
        self.assertFalse(o.video_evidence(task,A,allow_history=True))
        o.record_tool('bili_video_understand',analysis(),task_id='fixture',token='fixture-token')
        task=c.read_json(c.STATE_FILE,{})['owner_tasks']['fixture']
        self.assertTrue(o.video_evidence(task,A))
        self.assertNotIn(A,task['media_failures'])
        o.record_tool('bili_video_understand',{'ok':True,'bvid':A,'source':'subtitles'},task_id='fixture',token='fixture-token')
        self.assertFalse(o.video_evidence(c.read_json(c.STATE_FILE,{})['owner_tasks']['fixture'],A))

    async def test_historical_actions_complete_once_without_rewatch(self):
        self.start()
        v=SimpleNamespace(has_liked=AsyncMock(side_effect=[False,True]),like=AsyncMock(),set_favorite=AsyncMock())
        folders=[{'list':[{'id':7,'title':'默认收藏夹','fav_state':0}]},
                 {'list':[{'id':7,'title':'默认收藏夹','fav_state':1}]}]
        with patch('bilibili_api.video.Video',return_value=v),patch('bilibili_api.favorite_list.get_video_favorite_list',AsyncMock(side_effect=folders)):
            for action in ['like_video','favorite']:
                first=await o.action(self.cred,action,bvid=A,task_id='fixture',token='fixture-token')
                again=await o.action(self.cred,action,bvid=A,task_id='fixture',token='fixture-token')
                self.assertTrue(first['success']);self.assertTrue(again['duplicate'])
        v.like.assert_awaited_once();v.set_favorite.assert_awaited_once()
        with c.locked_state() as state:
            state['owner_tasks']['fixture']['proposed']={'status':'completed','text':'已点赞收藏'+A+'。'}
        with patch.object(c,'send_guarded',AsyncMock(return_value={'success':True,'rpid_str':'102'})) as send:
            result=await o.dispatch(self.cred,'finalize','fixture','fixture-token','[]',True)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('没有取得真实',send.call_args.args[1])
        self.assertEqual(len(c.read_json(c.STATE_FILE,{})['owner_tasks']['fixture']['actions']),2)

    async def test_missing_new_watch_and_uncertain_actions_still_fail(self):
        for text,action in [('去看'+A,{'status':'success','params':{'bvid':A,'action':'like_video'}}),
                            (self.incoming['text'],{'status':'uncertain','params':{'bvid':A,'action':'favorite'}})]:
            self.incoming['text']=text;self.task['actions']={'once':action}
            self.task['proposed']={'status':'completed','text':'已点赞收藏'+A+'。'}
            self.start()
            with patch.object(c,'send_guarded',AsyncMock(return_value={'success':True})):
                result=await o.dispatch(self.cred,'finalize','fixture','fixture-token','[]',True)
            self.assertEqual(result['status'],'failed')

    async def test_actual_and_historical_watch_claims_finalize_with_bound_proof(self):
        for fresh in [False,True]:
            with self.subTest(fresh=fresh):
                self.incoming['text']=('观看'+A) if fresh else '给之前看过的视频点赞收藏'
                self.task['proposed']={'status':'completed','text':'已经看完'+A+'，画面中有一个蓝色角色。'}
                self.task['executed_mcp']=['bili_video_understand' if fresh else 'bili_history_candidates']
                self.start();self.store()
                if fresh:o.record_tool('bili_video_understand',analysis(),task_id='fixture',token='fixture-token')
                with patch.object(c,'send_guarded',AsyncMock(return_value={'success':True})):
                    result=await o.dispatch(self.cred,'finalize','fixture','fixture-token','[]',True)
                self.assertEqual(result['status'],'completed')
                saved=c.read_json(c.STATE_FILE,{})['owner_tasks']['fixture']
                self.assertTrue(saved['media_verification']['ok'])
                self.assertNotIn('token',saved)

    def test_no_cross_account_cache_or_owner_name_authorization(self):
        self.store();self.start()
        with patch.object(c,'STATE_FILE',self.home/'other-account'/'conversations.json'):
            self.assertFalse(o.video_evidence(self.task,A,allow_history=True))
        with c.locked_state() as state:
            state['pending']['101']['uid']='900099'
        with self.assertRaises(PermissionError):
            o.record_tool('bili_video_understand',analysis(),task_id='fixture',token='fixture-token')


if __name__=='__main__':unittest.main(verbosity=2)
