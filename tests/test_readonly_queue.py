import json,os,sys,tempfile,unittest,time
from pathlib import Path
from unittest.mock import AsyncMock,patch
sys.path.insert(0,os.environ.get('BILI_TEST_VENDOR',str(Path(__file__).resolve().parents[1]/'vendor'/'bilibili-mcp')))
import conversation as c,private_messages as dm

class ReadonlyQueue(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.home=Path(self.temp.name)
  for name,value in [('STATE_FILE',self.home/'conversations.json'),('CONFIG_FILE',self.home/'config.json')]:
   change=patch.object(c,name,value);change.start();self.addCleanup(change.stop)
  c.CONFIG_FILE.write_text('{"replyToPrivateMessages":true}',encoding='utf-8')
  state=c.new_state();state['sent']['old']='receipt';c.save_state(state)
 def test_readonly_snapshot_never_saves_or_persists_mutations(self):
  before=c.STATE_FILE.read_bytes()
  with patch.object(c,'save_state',side_effect=AssertionError('read-only call wrote state')):
   with c.locked_state(read_only=True) as snapshot:snapshot['sent']['temporary']='local'
  self.assertEqual(c.STATE_FILE.read_bytes(),before)
  with c.locked_state() as state:state['sent']['new']='receipt'
  self.assertEqual(c.read_json(c.STATE_FILE,{})['sent'],{'old':'receipt','new':'receipt'})
 async def test_cached_private_query_has_no_save_or_network(self):
  before=c.STATE_FILE.read_bytes()
  with patch.object(c,'save_state',side_effect=AssertionError('queue query wrote state')),patch.object(dm,'poll',AsyncMock(side_effect=AssertionError('network'))):
   result=await dm.pending_private_messages(object(),refresh=False)
  self.assertEqual(result['queued'],0);self.assertEqual(c.STATE_FILE.read_bytes(),before)
 def test_readonly_exceptions_release_mutex_without_changing_history(self):
  with self.assertRaises(ValueError):
   with c.locked_state(read_only=True):raise ValueError('cancelled query')
  with c.locked_state() as state:state['sent']['after']='receipt'
  self.assertEqual(c.read_json(c.STATE_FILE,{})['sent'],{'old':'receipt','after':'receipt'})
 def test_corrupt_history_fails_closed_instead_of_resetting(self):
  c.STATE_FILE.write_text('{broken',encoding='utf-8')
  with self.assertRaises(ValueError):
   with c.locked_state(read_only=True):pass
  self.assertEqual(c.STATE_FILE.read_text('utf-8'),'{broken')

if __name__=='__main__':unittest.main(verbosity=2)
