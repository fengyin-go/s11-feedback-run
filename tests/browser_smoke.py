"""可选真实浏览器/编码验证；仅本机 HTTP 和无害命令，不使用 Docker/模型。"""
import sys, tempfile, subprocess, json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fbr_web, fbr_record
out=Path(tempfile.mkdtemp(prefix='s11-web-verified-'))
port=fbr_web.free_port()
(out/'index.html').write_text("<input id=q><button onclick=\"document.querySelector('#result').textContent=document.querySelector('#q').value\">Go</button><div id=result></div>")
(out/'details.html').write_text("<h1>Details</h1><input id=label><button onclick=\"document.querySelector('#saved').textContent=document.querySelector('#label').value\">Save</button><div id=saved></div><a href='/runs.html'>Runs</a>")
(out/'runs.html').write_text("<h1>Runs</h1><button onclick=\"document.querySelector('#status').textContent='Done'\">Run</button><div id=status>Idle</div>")
with (out/'index.html').open('a') as page:
    page.write("<a href='/details.html'>Details</a>")
original=subprocess.Popen
def local_only(command, **kwargs):
    if command[:2] == ['docker','exec']:
        return original([sys.executable,'-u','-m','http.server',str(port),'--bind','127.0.0.1','--directory',str(out)],**kwargs)
    return original(command,**kwargs)
plan={'serve_cmd':'fixture-server {port}', 'steps':[{'action':'fill','selector':'#q','value':'verified'}, {'action':'click','selector':'button'}, {'action':'expect_text','selector':'#result','text':'verified','pause_ms':1500},
    {'action':'click','selector':"a[href='/details.html']"},
    {'action':'fill','selector':'#label','value':'updated'},
    {'action':'click','selector':'button'},
    {'action':'expect_text','selector':'#saved','text':'updated','pause_ms':1500},
    {'action':'click','selector':"a[href='/runs.html']"},
    {'action':'click','selector':'button'},
    {'action':'expect_text','selector':'#status','text':'Done','pause_ms':1500}]}
with patch.object(subprocess,'Popen',side_effect=local_only):
    steps,metadata=fbr_web.render(plan,'no-container-used',port,out,{'width':1280,'height':720,'max_seconds':40})
fbr_record.to_mp4(out/'raw.webm',out/'recording.mp4',40,1280,720)
video=fbr_record.probe_mp4(out/'recording.mp4')
assert metadata['startup_ready'] and metadata['page_segment'] and not metadata['page_errors'],metadata
assert steps[0]['input_recorded'] and metadata['command_input_seconds'] > 0, (steps, metadata)
assert all(s['rc']==0 for s in steps[1:]),steps
assert len({s['url'] for s in steps[1:]}) == 3,steps
assert [s['pause_ms'] for s in steps[1:] if s['cmd'].startswith('expect_text')] == [1500,1500,1500],steps
assert 16 <= video['duration'] <= 40,video
report={'directory':str(out),'metadata':metadata,'steps':steps,'video':video,'docker_and_model_calls':0}
(out/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))

terminal=out/'terminal'
terminal.mkdir()
terminal_steps=fbr_record.render([{'cmd': "printf '终端录制验证\\n'", 'show':'终端实际输出'}], [], terminal,
    {'width':1280,'height':720,'max_seconds':15,'step_timeout':5})
fbr_record.to_mp4(terminal/'raw.webm',terminal/'recording.mp4',15,1280,720)
terminal_video=fbr_record.probe_mp4(terminal/'recording.mp4')
assert terminal_steps[0]['rc']==0 and not terminal_steps[0]['cut']
assert terminal_steps[0]['input_recorded'] and terminal_steps[0]['input_seconds'] > 0, terminal_steps
assert terminal_video['width']==1280 and terminal_video['height']==720 and terminal_video['duration']>0
print(json.dumps({'terminal_directory':str(terminal),'video':terminal_video},ensure_ascii=False))
