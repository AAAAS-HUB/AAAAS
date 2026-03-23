from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from openai import OpenAI
import os
import json
import time
import random
from datetime import date
from pathlib import Path

app = FastAPI(title="简历优化+文案SaaS")

# ================== 配置 ==================
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
FREE_LIMIT = 3
VIP_PRICE = 19.9
ORDER_PREFIX = "ORDER_"

client = OpenAI(
    api_key=DOUBAO_API_KEY,
    base_url="https://ark.cn-beijing.volces.com/api/v3"
)
MODEL_NAME = "doubao-2.0-lite"

# ================== 数据文件 ==================
DATA_FILE = Path("data/users.json")
if not DATA_FILE.exists():
    DATA_FILE.parent.mkdir(exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

def load_users():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ================== 工具 ==================
def today():
    return str(date.today())

def get_user(phone):
    users = load_users()
    return users.get(phone)

def create_user_if_not_exists(phone):
    users = load_users()
    if phone not in users:
        users[phone] = {
            "code": None,
            "code_expire": 0,
            "vip": False,
            "vip_expire": 0,
            "date": today(),
            "daily_count": 0
        }
        save_users(users)
    return users[phone]

def check_and_use_count(phone):
    users = load_users()
    u = users.get(phone)
    if not u:
        return False

    # 会员无限用
    if u.get("vip"):
        return True

    # 每日重置
    if u["date"] != today():
        u["date"] = today()
        u["daily_count"] = 0

    if u["daily_count"] >= FREE_LIMIT:
        return False

    u["daily_count"] += 1
    save_users(users)
    return True

# ================== 前端页面 ==================
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI简历·文案SaaS</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui}
body{max-width:800px;margin:2rem auto;padding:0 1rem}
.login-box{background:#f8fafc;padding:1.2rem;border-radius:12px;margin-bottom:1rem}
input,textarea{width:100%;padding:0.8rem;margin:0.5rem 0;border:1px solid #ddd;border-radius:8px}
button{padding:0.8rem;border:none;border-radius:8px;background:#2563eb;color:white;cursor:pointer}
.btn-vip{background:#f59e0b}
.tab{display:flex;gap:0.6rem;margin:1rem 0}
.tab button{background:#f1f5f9}
.tab button.active{background:#2563eb;color:white}
.panel{display:none}
.panel.active{display:block}
.result{margin-top:1rem;padding:1rem;background:#f3f4f6;border-radius:8px;white-space:pre-wrap}
.vip-tip{padding:0.8rem;background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;margin:0.5rem 0}
</style>

<div class="login-box">
  <div id="login_panel">
    <input id="phone" placeholder="手机号" type="tel">
    <button onclick="sendCode()" style="width:100%">获取验证码</button>
    <input id="code" placeholder="验证码">
    <button onclick="login()" style="width:100%">登录</button>
  </div>
  <div id="user_panel" style="display:none">
    <p>用户：<span id="show_phone"></span></p >
    <p id="user_info"></p >
    <div id="limit_tip" class="vip-tip" style="display:none">今日免费次数已用完</div>
    <button class="btn-vip" onclick="toPay()" style="width:100%">开通会员 19.9 元/月</button>
  </div>
</div>

<div class="tab">
  <button class="active" onclick="show('resume')">简历优化</button>
  <button onclick="show('copy')">文案生成</button>
</div>

<div id="resume" class="panel active">
<textarea id="r_content" placeholder="粘贴你的简历" style="height:140px"></textarea>
<input id="r_jd" placeholder="目标岗位JD（可选）">
<input id="r_job" value="产品经理" placeholder="目标岗位">
<button onclick="goResume()" style="width:100%">优化简历</button>
<div id="r_result" class="result"></div>
</div>

<div id="copy" class="panel">
<input id="c_topic" placeholder="文案主题">
<input id="c_style" value="正式" placeholder="风格">
<input id="c_len" value="200字" placeholder="字数">
<button onclick="goCopy()" style="width:100%">生成文案</button>
<div id="c_result" class="result"></div>
</div>

<script>
let phone = null;
function show(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'))
  document.querySelectorAll('.tab button').forEach(b=>b.classList.remove('active'))
  document.getElementById(id).classList.add('active')
  event.target.classList.add('active')
}

async function sendCode(){
  const p = document.getElementById('phone').value
  if(!p)return alert('请输入手机号')
  const r = await fetch('/api/send-code?phone='+p)
  const d = await r.json()
  alert(d.ok ? '验证码已发送（控制台可见）':'失败')
}

async function login(){
  const p = document.getElementById('phone').value
  const c = document.getElementById('code').value
  const r = await fetch('/api/login?phone='+p+'&code='+c)
  const d = await r.json()
  if(d.ok){
    phone = p
    document.getElementById('login_panel').style.display='none'
    document.getElementById('user_panel').style.display='block'
    document.getElementById('show_phone').innerText = phone
    refreshUser()
  }else alert('验证码错误')
}

async function refreshUser(){
  const r = await fetch('/api/user-info?phone='+phone)
  const d = await r.json()
  document.getElementById('user_info').innerText = d.vip ? '会员已开通' : `今日剩余 ${d.left}/${FREE_LIMIT} 次`
  document.getElementById('limit_tip').style.display = d.left<1 && !d.vip ? 'block':'none'
}

async function goResume(){
  if(!phone)return alert('请登录')
  await refreshUser()
  const btn = event.target;btn.disabled=true;btn.innerText='生成中...'
  const r = await fetch('/api/resume',{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:`phone=${phone}&content=${encodeURIComponent(document.getElementById('r_content').value)}&jd=${encodeURIComponent(document.getElementById('r_jd').value)}&job=${encodeURIComponent(document.getElementById('r_job').value)}`
  })
  const d = await r.json()
  document.getElementById('r_result').innerText = d.data || d.error
  btn.disabled=false;btn.innerText='优化简历'
  await refreshUser()
}

async function goCopy(){
  if(!phone)return alert('请登录')
  await refreshUser()
  const btn = event.target;btn.disabled=true;btn.innerText='生成中...'
  const r = await fetch('/api/copy',{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:`phone=${phone}&topic=${encodeURIComponent(document.getElementById('c_topic').value)}&style=${encodeURIComponent(document.getElementById('c_style').value)}&len=${encodeURIComponent(document.getElementById('c_len').value)}`
  })
  const d = await r.json()
  document.getElementById('c_result').innerText = d.data || d.error
  btn.disabled=false;btn.innerText='生成文案'
  await refreshUser()
}

function toPay(){
  if(!phone)return aler