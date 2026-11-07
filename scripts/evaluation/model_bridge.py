import asyncio,json,sys,copy
import importlib.util
import semora_agent.model as model_module
spec=importlib.util.spec_from_file_location('isolated_diagnostics','/tmp/new_model_diagnostics.py');patched=importlib.util.module_from_spec(spec);spec.loader.exec_module(patched);model_module.read_response=patched.read_response
from app.model_settings import model_settings
from semora_agent.model import structured_model
async def main():
 p=json.load(sys.stdin); m=copy.copy(structured_model);m.timeout_seconds=120;m.request_retries=0
 traces=[]
 try:
  result=await m.complete_messages(p['messages'],p['tools'],tool_choice=p.get('tool_choice'),parallel_tool_calls=False,on_request_event=lambda n,d:traces.append({'event':n,**{k:d.get(k) for k in ['duration_ms','request_bytes','first_output_ms','model','reasoning_chars','tool_argument_chars','output_chars','chunks','finish_reason','finish_received_ms','stream_end','repeated_tool_ids']}}) if n in ['agent.request.completed','agent.request.timeout'] else None)
 except Exception as exc:
  print(json.dumps({'error_type':type(exc).__name__,'traces':traces}));return
 print(json.dumps({'result':result,'traces':traces}))
asyncio.run(main())
