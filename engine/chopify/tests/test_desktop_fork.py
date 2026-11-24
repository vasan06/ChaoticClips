"""Exercise fork policies and actual native-MP4 workflow through HTTP."""
import json
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
import pytest
import dashboard
import projects
import local_policy
import ingest

@pytest.fixture
def server(tmp_path):
    httpd=dashboard.make_server(tmp_path / "projects",0)
    worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown();httpd.app.close();httpd.server_close();worker.join()

def call(server,path,body=None):
    httpd,base=server
    headers={"X-Chopify-Token":httpd.app.token}
    data=None
    if body is not None:
        data=json.dumps(body).encode();headers["Content-Type"]="application/json"
    with urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=headers)) as result:
        return json.load(result)

def wait(server,project):
    deadline=time.monotonic()+80
    while time.monotonic()<deadline:
        detail=call(server,"/api/projects/"+project)
        if not detail["busy"]: return detail
        time.sleep(.1)
    raise TimeoutError("project did not finish")

def test_local_only_boundaries():
    assert local_policy.source("https://youtu.be/Wb8Tq_HD4E8")=="https://www.youtube.com/watch?v=Wb8Tq_HD4E8"
    for source in ("http://127.0.0.1/x","https://youtube.com.evil.example/watch?v=Wb8Tq_HD4E8","https://youtube.com@evil.example/x", "https://example.com/file"):
        with pytest.raises(ValueError): local_policy.source(source)
    with pytest.raises(ValueError): local_policy.local_path(local_policy.ROOT/".env")
    assert ingest.is_long_source({"url":True,"duration":40,"estimated_size":10})
    assert projects.validate_options({})["asr"]=="whisper"
    assert projects.validate_options({})["fit"]=="contain"

@pytest.fixture
def source(tmp_path):
    path=tmp_path/"source.mp4"
    subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=red:s=640x360:r=12:d=40","-f","lavfi","-i","sine=frequency=500:duration=40","-vf","drawbox=x=0:y=0:w=iw:h=ih:color=green:t=fill:enable='between(t,10,20)'","-c:v","libx264","-threads","2","-preset","ultrafast","-c:a","aac",str(path)],check=True,capture_output=True)
    return path

def test_upload_preview_cancel_export_and_edit_gate(server,source,tmp_path,monkeypatch):
    httpd,base=server
    boundary="local-fork-upload"
    body=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"source.mp4\"\r\nContent-Type: video/mp4\r\n\r\n".encode()+source.read_bytes()+f"\r\n--{boundary}--\r\n".encode())
    request=urllib.request.Request(base+"/api/upload",data=body,headers={"X-Chopify-Token":httpd.app.token,"Content-Type":"multipart/form-data; boundary="+boundary})
    with urllib.request.urlopen(request) as response: uploaded=json.load(response)
    assert Path(uploaded["source"]).read_bytes()==source.read_bytes()
    project=call(server,"/api/projects",{"source":uploaded["source"],"options":{"aspect":"9:16","fit":"contain"}})
    key=project["id"];path=projects.project_path(httpd.app.root,key)
    state=projects.read_project(path);state["video"]=uploaded["source"];projects.save_project(path,state)
    projects.atomic_json(path/"transcript.json",{"video":uploaded["source"],"duration":40,"words":[{"word":"Known source moment.","start":12.1,"end":17.8}]})
    projects.atomic_json(path/"segments.json",[{"start":12,"end":18,"hook":"Known green frame","overall":8.5}])
    prefix="/api/projects/"+key
    with pytest.raises(urllib.error.HTTPError) as error: call(server,prefix+"/run",{"action":"render","preview":False,"pick":"1"})
    assert error.value.code==400
    call(server,prefix+"/run",{"action":"render","preview":True,"pick":"1"})
    detail=wait(server,key)
    assert detail["project"]["status"]!="failed",detail["project"].get("error")
    previews=list((path/"previews").glob("*.mp4"));assert len(previews)==1
    raw=subprocess.run(["ffmpeg","-v","error","-ss","1","-i",str(previews[0]),"-frames:v","1","-vf","crop=2:2:350:630","-f","rawvideo","-pix_fmt","rgb24","pipe:1"],check=True,capture_output=True).stdout
    assert raw[1]>raw[0]+60 and raw[1]>raw[2]+60
    bars=subprocess.run(["ffmpeg","-v","error","-ss","1","-i",str(previews[0]),"-frames:v","1","-vf","crop=2:2:350:20","-f","rawvideo","-pix_fmt","rgb24","pipe:1"],check=True,capture_output=True).stdout
    assert max(bars[:3])<10, "Project rendering ignored full-picture layout"
    media_url=next(f["url"] for f in detail["files"] if f["name"].endswith(".mp4"))
    with urllib.request.urlopen(urllib.request.Request(base+media_url,headers={"Range":"bytes=0-127"})) as response:
        assert response.status==206 and len(response.read())==128
    monkeypatch.setattr(local_policy,"choose",lambda kind:None)
    with pytest.raises(urllib.error.HTTPError) as error: call(server,prefix+"/run",{"action":"render","preview":False,"pick":"1"})
    assert error.value.code==400 and not (path/"clips").exists()
    destination=tmp_path/"chosen-folder";destination.mkdir()
    monkeypatch.setattr(local_policy,"choose",lambda kind:destination)
    call(server,prefix+"/run",{"action":"render","preview":False,"pick":"1"})
    result=wait(server,key)
    assert result["project"]["status"]!="failed",result["project"].get("error")
    final=list(destination.glob("*.mp4"));assert len(final)==1
    info=json.loads(subprocess.run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(final[0])],check=True,capture_output=True,text=True).stdout)
    assert abs(float(info["format"]["duration"])-6)<.15
    assert {s["codec_name"] for s in info["streams"]}>={"h264","aac"}
    assert list(destination.glob("*.srt"))
    call(server,prefix+"/candidates",{"candidates":[{"start":24,"end":30,"hook":"Changed moment","overall":8.5}]})
    with pytest.raises(urllib.error.HTTPError): call(server,prefix+"/run",{"action":"render","preview":False,"pick":"1"})


def test_dashboard_subprocess_cannot_wait_for_console_input(tmp_path,monkeypatch):
    source=tmp_path/"source.mp4";source.write_bytes(b"fixture")
    studio=dashboard.Studio(tmp_path/"projects")
    state=projects.create_project(studio.root,str(source),{})
    captured={}
    class Child:
        def __init__(self,cmd,**kwargs):captured.update(cmd=cmd,**kwargs)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def wait(self):return 0
    monkeypatch.setattr(dashboard.subprocess,"Popen",Child)
    try:studio._run(projects.project_path(studio.root,state["id"]),"prepare",False,None)
    finally:studio.close()
    assert "--no-input" in captured["cmd"]
    assert captured["stdin"]==subprocess.DEVNULL


def test_tamil_marks_are_preserved_in_hooks_and_filenames():
    import score_clips, render_clips
    assert score_clips._norm_token("எப்படி?") == "எப்படி"
    assert render_clips.sanitize("நீங்கள் எப்படி") == "நீங்கள்-எப்படி"


def test_full_source_cache_is_reused_across_projects(tmp_path,monkeypatch):
    import sections
    monkeypatch.setenv("CHOPIFY_SOURCE_CACHE",str(tmp_path/"cache"))
    calls=[]
    def download(url,path,quality=1080,runner=None):
        calls.append(url);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"cached media");return path
    monkeypatch.setattr(ingest,"download_full",download)
    first=sections.ensure_full_source("https://youtu.be/Wb8Tq_HD4E8",tmp_path/"first")
    second=sections.ensure_full_source("https://youtu.be/Wb8Tq_HD4E8",tmp_path/"second")
    assert first==second and len(calls)==1


def test_caption_parser_preserves_tamil_offsets_and_reports_estimates():
    import download_and_transcribe as dt
    words,segments,estimated=dt._parse_source_captions({"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"நீங்கள்","tOffsetMs":0},{"utf8":"எப்படி?","tOffsetMs":1000}]}]},10)
    assert [w["word"] for w in words]==["நீங்கள்","எப்படி?"]
    assert words[0]["start"]==1 and words[1]["start"]==2 and not estimated
    words,segments,estimated=dt._parse_source_captions({"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"நீங்கள் எப்படி"}]}]},10)
    assert estimated and len(words)==2
    assert dt._parse_source_captions({"events":[{"tStartMs":"NaN","dDurationMs":2000,"segs":[{"utf8":"bad"}]}]},10)[0]==[]
