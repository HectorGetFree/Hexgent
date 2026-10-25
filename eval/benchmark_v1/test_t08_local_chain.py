"""完整T08本地协议回归：仅在SDK边界替换模型，真实HTTP/SQLite/SIGKILL/恢复/评分。"""
import unittest
import json,sys,runpy,subprocess,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
REPO=Path(__file__).resolve().parents[2];sys.path.insert(0,str(REPO))
class Item(SimpleNamespace):
 def model_dump(self,**kw):return vars(self)
class OfflineResponses:
 def __init__(self):self.n=0
 def create(self,**kw):
  self.n+=1
  restore='--restore' in sys.argv
  step=self.n
  name=('fixture_read' if step==1 else None) if restore else ('fixture_read' if step==1 else 'fixture_ship')
  args={'order_id':'ORDER-41'}
  if name=='fixture_ship':args['operation_id']='SHIP-41'
  output=[Item(type='function_call',name=name,call_id=('after' if restore else 'before')+str(step),arguments=json.dumps(args))] if name else []
  response=SimpleNamespace(id='offline-'+str(step),output=output,output_text='' if name else 'verified',usage={'input_tokens':100,'output_tokens':20})
  return iter([SimpleNamespace(type='response.completed',response=response)])
class OfflineClient:
 def __init__(self):self.responses=OfflineResponses();self.base_url='http://offline.invalid'
 def with_options(self,**kw):return self


class T08LocalChainTest(unittest.TestCase):
 def test_scripted_model_through_real_fixture_crash_restore_and_verifier(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);envfile=root/'offline.env'
   envfile.write_text('OPENAI_API_KEY=offline-only\nOPENAI_BASE_URL=http://offline.invalid\nMODEL_ID=offline-scripted\n')
   original=subprocess.Popen
   def launch(command,*a,**kw):
    if isinstance(command,list) and 'eval.benchmark_v1.fixture_agent' in command:
     index=command.index('eval.benchmark_v1.fixture_agent')
     command=[sys.executable,__file__,'child',*command[index+1:]]
     kw['stderr']=subprocess.DEVNULL
    return original(command,*a,**kw)
   argv=['fixture_smoke','--revision','f1','--variant','baseline','--case','t08-crash','--trial-id','t08-local-chain-check','--root',str(root/'smoke'),'--provider-env-file',str(envfile)]
   from .fixture_smoke import main
   with patch.object(sys,'argv',argv),patch('subprocess.Popen',launch):main()
   result=json.loads((root/'smoke/protocol-result-v2.json').read_text())
   self.assertEqual((result['protocol_status'],result['task_status']),('PASS','PASS'))
   self.assertEqual(result['integrity']['actual_dispatches'],3)
   self.assertTrue(result['scenario_triggered'])
   lifecycle=json.loads((root/'smoke/lifecycle.json').read_text())
   self.assertEqual(lifecycle['destroy'],'PASS');self.assertEqual(lifecycle['errors'],{})
   self.assertFalse((root/'smoke/private').exists())


if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='child':
  sys.argv=[sys.argv[0],*sys.argv[2:]]
  client=OfflineClient()
  with patch('hexgent.client.get_client',return_value=client),patch('hexgent.client.get_model',return_value='offline-scripted'),patch('hexgent.loop.agent_turn.get_model',return_value='offline-scripted'):
   runpy.run_module('eval.benchmark_v1.fixture_agent',run_name='__main__')
 else:unittest.main()
