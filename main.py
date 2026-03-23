from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from openai import OpenAI
import os

app = FastAPI()

# 豆包 API（火山方舟兼容 OpenAI 格式）
client = OpenAI(
    api_key=os.getenv("DOUBAO_API_KEY"),
    base_url="https://ark.cn-beijing.volces.com/api/v3"
)

MODEL_NAME = "doubao-2.0-lite"


# 首页
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI简历优化·文案生成</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto}
body{max-width:800px;margin:30px auto;padding:0 15px}
.tab{display:flex;gap:10px;margin-bottom:20px}
.tab button{padding:10px 16px;border-radius:8px;border:none;cursor:pointer;background:#f1f5f9}
.tab button.active{background:#2563eb;color:white}
.panel{display:none}
.panel.active{display:block}
.card{padding:16px;border-radius:10px;background:#f8fafc;margin-bottom:12px}
input,textarea{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:8px;margin-bottom:10px}
textarea{min-height:140px}
.btn-submit{width:100%;padding:14px;background:#2563eb;color:white;border:none;border-radius:8px;font-size:16px;cursor:pointer}
.result{margin-top:16px;padding:16px;background:#f1f5f9;border-radius:8px;white-space:pre-wrap;line-height:1.6}
</style>
</head>
<body>
<h1 style="margin-bottom:20px">AI 简历优化 & 文案生成</h1>

<div class="tab">
<button class="active" onclick="show('resume')">简历优化</button>
<button onclick="show('copy')">文案生成</button>
</div>

<div id="resume" class="panel active">
<div class="card">
<textarea id="r_content" placeholder="粘贴你的原始简历内容"></textarea>
<input id="r_jd" placeholder="目标岗位JD（可选）">
<input id="r_job" value="产品经理" placeholder="目标岗位">
<button class="btn-submit" onclick="doResume()">优化简历</button>
<div id="r_result" class="result"></div>
</div>
</div>

<div id="copy" class="panel">
<div class="card">
<input id="c_topic" placeholder="文案主题：例如 奶茶店活动、产品介绍、朋友圈">
<input id="c_style" value="正式" placeholder="风格：正式/活泼/口语/高级">
<input id="c_len" value="200字" placeholder="字数：例如 150字、300字">
<button class="btn-submit" onclick="doCopy()">生成文案</button>
<div id="c_result" class="result"></div>
</div>
</div>

<script>
function show(id){
document.querySelectorAll('.panel').forEach(e=>e.classList.remove('active'))
document.querySelectorAll('.tab button').forEach(e=>e.classList.remove('active'))
document.getElementById(id).classList.add('active')
event.target.classList.add('active')
}

async function doResume(){
const btn = event.target
btn.disabled = true
btn.textContent = '生成中...'
const res = await fetch('/api/resume',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'content='+encodeURIComponent(document.getElementById('r_content').value)+
'&jd='+encodeURIComponent(document.getElementById('r_jd').value)+
'&job='+encodeURIComponent(document.getElementById('r_job').value)
})
const data = await res.json()
document.getElementById('r_result').textContent = data.data || data.error
btn.disabled = false
btn.textContent = '优化简历'
}

async function doCopy(){
const btn = event.target
btn.disabled = true
btn.textContent = '生成中...'
const res = await fetch('/api/copy',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'topic='+encodeURIComponent(document.getElementById('c_topic').value)+
'&style='+encodeURIComponent(document.getElementById('c_style').value)+
'&len='+encodeURIComponent(document.getElementById('c_len').value)
})
const data = await res.json()
document.getElementById('c_result').textContent = data.data || data.error
btn.disabled = false
btn.textContent = '生成文案'
}
</script>
</body>
</html>
"""


# 简历优化接口
@app.post("/api/resume")
def api_resume(
    content: str = Form(...),
    jd: str = Form(""),
    job: str = Form("产品经理")
):
    prompt = f"""
你是专业简历优化师，严格使用STAR法则，突出成果、数据、动词开头，不编造。
目标岗位：{job}
岗位JD：{jd}

原始简历：
{content}

请直接输出优化后的专业简历，分点清晰、简洁有力：
"""
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return {"data": resp.choices[0].message.content}
    except Exception as e:
        return {"error": str(e)}


# 文案生成接口
@app.post("/api/copy")
def api_copy(
    topic: str = Form(...),
    style: str = Form("正式"),
    len: str = Form("200字")
):
    prompt = f"""
请生成一篇文案，满足：
主题：{topic}
风格：{style}
字数：{len}
语言自然流畅，直接输出正文即可。
"""
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8
        )
        return {"data": resp.choices[0].message.content}
    except Exception as e:
        return {"error": str(e)}