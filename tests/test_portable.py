import json,os,sys,tempfile,subprocess,unittest
from pathlib import Path
VENDOR=Path(__file__).resolve().parents[1]/'vendor'/'bilibili-mcp'

class PortableIdentity(unittest.TestCase):
 def run_profile(self,identity,code):
  with tempfile.TemporaryDirectory() as folder:
   home=Path(folder)
   if identity is not None:(home/'identity.json').write_text(json.dumps(identity),'utf-8')
   env={**os.environ,'BILI_DUTY_HOME':folder,'PYTHONUTF8':'1','PYTHONPATH':str(VENDOR)}
   result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
   self.assertEqual(result.returncode,0,result.stderr)
 def test_two_different_installations_and_impersonation(self):
  for uid in ['900001','800001']:
   self.run_profile({'ownerUid':uid,'botUid':'900002'},f"import conversation as c; assert c.is_owner({{'uid':'{uid}'}}); assert not c.is_owner({{'uid':'900099','name':'主人'}}); assert c.BOT_UID=='900002'")
 def test_unbound_and_malformed_uid_are_closed(self):
  for identity in [None,{}, {'ownerUid':[], 'botUid':{}}, {'ownerUid':'0','botUid':'same-name'}]:
   self.run_profile(identity,"import conversation as c; assert not c.is_owner({'uid':''}); assert not c.is_owner({'uid':'900001'})")
 def test_proactive_setting_uses_the_shared_switch(self):
  self.run_profile({'ownerUid':'900001','botUid':'900002'},"import conversation as c,owner_checkins as o; c.HOME=__import__('pathlib').Path(__import__('os').environ['BILI_DUTY_HOME']); (c.HOME/'config.json').write_text('{\"ownerCheckinsEnabled\":false}'); assert o.eligible(c.new_state())['reason']=='disabled'")

if __name__=='__main__':unittest.main()
