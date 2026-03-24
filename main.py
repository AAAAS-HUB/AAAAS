from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
import os
import json
import time
import random
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ================== 基础配置 ==================
app = FastAPI(title="AI简历·文案SaaS Pro")

# 跨域 + 限流配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 配置项
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
FREE_LIMIT = 3
VIP_PACKAGES = {  # 会员套餐：key=天数, value=价格
    "month": {"days": 30, "price": 19.9},
    "season": {"days": 90, "price": 49.9},
    "year": {"days": 365, "price": 169.9}
}
ORDER_PREFIX = "PAY_"
WXPAY_QR_URL = "https://s41.ax1x.com/2026/03/23/peKuPxg.jpg"
SMS_API_KEY = os.getenv("SMS_API_KEY", "")  # 短信平台API Key（如阿里云/腾讯云）

# OpenAI客户端
client = OpenAI(
    api_key=DOUBAO_API_KEY,
    base_url="https://ark.cn-beijing.volces.com/api/v3"
)
MODEL_NAME = "doubao-2.0-lite"

# ================== 数据存储 ==================
DATA_DIR = Path("data")
USERS_FILE = DATA_DIR / "users.json"
HISTORY_FILE = DATA_DIR / "history.json"

# 初始化目录和文件
for file in [USERS_FILE, HISTORY_FILE]:
    if not file.exists():
        file.parent.mkdir(exist_ok=True)
        with open(file, "w", encoding="utf-8") as f:
            json.dump({}, f)

# ================== 工具函数 ==================
def encrypt_phone(phone: str) -> str:
    """手机号加密存储"""
    return hashlib.md5(phone.encode("utf-8")).hexdigest()[:16]

def today() -> str:
    return str(date.today())

def now() -> int:
    """当前时间戳"""
    return int(time.time())

def format_time(timestamp: int) -> str:
    """时间戳转格式化字符串"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")

def send_sms_code(phone: str, code: str) -> bool:
    """发送短信验证码（对接真实短信平台）"""
    # 示例：替换为阿里云/腾讯云短信API调用
    if not SMS_API_KEY:
        print(f"【调试模式】向{phone}发送验证码：{code}")
        return True
    # 真实短信平台调用示例（需根据平台文档调整）
    # import requests
    # resp = requests.post(
    #     "https://sms-api.example.com/send",
    #     json={"phone": phone, "code": code, "api_key": SMS_API_KEY}
    # )
    # return resp.json().get("success", False)
    return True

def filter_sensitive_content(content: str) -> str:
    """过滤敏感内容"""
    sensitive_words = ["暴力", "色情", "赌博"]  # 可扩展敏感词库
    for word in sensitive_words:
        content = content.replace(word, "*" * len(word))
    return content

# ================== 用户数据操作 ==================
def load_users() -> dict:
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(data: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_history() -> dict:
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_history(data: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def init_user(phone: str) -> dict:
    """初始化用户数据"""
    users = load_users()
    enc_phone = encrypt_phone(phone)
    if enc_phone not in users:
        users[enc_phone] = {
            "phone": phone,  # 可改为仅存加密后手机号（更安全）
            "code": None,
            "code_expire": 0,
            "vip": False,
            "vip_expire": 0,  # 会员到期时间戳
            "vip_package": "",  # 会员套餐类型
            "date": today(),
            "daily_count": 0,
            "invite_code": f"INV{random.randint(100000, 999999)}",  # 邀请码
            "invited_by": ""  # 被谁邀请
        }
        save_users(users)
    return users[enc_phone]

def check_vip_status(phone: str) -> bool:
    """检查会员是否有效"""
    users = load_users()
    enc_phone = encrypt_phone(phone)
    u = users.get(enc_phone)
    if not u:
        return False
    # 会员未过期则自动续期标记
    if u.get("vip_expire", 0) > now():
        u["vip"] = True
    else:
        u["vip"] = False
    save_users(users)
    return u["vip"]

def check_use_limit(phone: str) -> tuple[bool, str]:
    """检查使用限制（返回：是否可用，提示信息）"""
    users = load_users()
    enc_phone = encrypt_phone(phone)
    u = users.get(enc_phone)
    if not u:
        return False, "用户未登录"
    
    # 会员直接放行
    if check_vip_status(phone):
        return True, "会员不限次数"
    
    # 非会员检查每日免费次数
    if u["date"] != today():
        u["date"] = today()
        u["daily_count"] = 0
    if u["daily_count"] >= FREE_LIMIT:
        return False, f"今日免费次数已用完（{FREE_LIMIT}次），开通会员继续使用"
    u["daily_count"] += 1
    save_users(users)
    return True, f"今日剩余免费次数：{FREE_LIMIT - u['daily_count']}"

def activate_vip(phone: str, package: str) -> bool:
    """开通会员套餐"""
    if package not in VIP_PACKAGES:
        return False
    users = load_users()
    enc_phone = encrypt_phone(phone)
    u = users.get(enc_phone)
    if not u:
        return False
    
    # 计算到期时间
    days = VIP_PACKAGES[package]["days"]
    current_expire = u.get("vip_expire", 0)
    new_expire = now() + days * 86400 if current_expire < now() else current_expire + days * 86400
    
    u["vip"] = True
    u["vip_expire"] = new_expire
    u["vip_package"] = package
    save_users(users)
    return True

def add_history(phone: str, type_: str, input_data: dict, output: str):
    """添加使用历史"""
    history = load_history()
    enc_phone = encrypt_phone(phone)
    if enc_phone not in history:
        history[enc_phone] = []
    history[enc_phone].append({
        "id": f"H{now()}{random.randint(100, 999)}",
        "type": type_,  # resume/copy
        "input": input_data,
        "output": output,
        "time": now()
    })
    # 只保留最近50条记录
    history[enc_phone] = history[enc_phone][-50:]
    save_history(history)

def get_history(phone: str) -> list:
    """获取用户历史记录"""
    history = load_history()
    enc_phone = encrypt_phone(phone)
    return history.get(enc_phone, [])

# ================== 前端页面（升级版） ==================
@app.get("/", response_class=HTMLResponse)
def index():
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI简历·文案SaaS Pro</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:system-ui}}
body{{max-width:800px;margin:2rem auto;padding:0 1rem;background:#f9fafb}}
.card{{background:white;padding:1.5rem;border-radius:16px;margin-bottom:1.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.05)}}
.login{{background:#f8fafc;padding:1.2rem;border-radius:12px;margin-bottom:1rem}}
input,textarea,select{{width:100%;padding:0.8rem;margin:0.5rem 0;border:1px solid #e2e8f0;border-radius:8px;font-size:16px}}
textarea{{resize:vertical;min-height:120px}}
button{{padding:0.8rem;border:none;border-radius:8px;background:#2563eb;color:white;cursor:pointer;font-size:16px;transition:background 0.2s}}
button:hover{{background:#1d4ed8}}
button:disabled{{background:#94a3b8;cursor:not-allowed}}
.btn-vip{{background:#f59e0b}}
.btn-vip:hover{{background:#d97706}}
.tab{{display:flex;gap:0.6rem;margin:1rem 0;flex-wrap:wrap}}
.tab button{{background:#f1f5f9;color:#64748b}}
.tab button.active{{background:#2563eb;color:white}}
.panel{{display:none}}
.panel.active{{display:block;animation:fadeIn 0.3s}}
.result{{margin-top:1rem;padding:1rem;background:#f3f4f6;border-radius:8px;white-space:pre-wrap;position:relative}}
.result .copy-btn{{position:absolute;top:0.8rem;right:0.8rem;padding:0.4rem 0.8rem;font-size:14px;background:#2563eb;color:white;border-radius:4px;cursor:pointer}}
.vip-tip{{padding:0.8rem;background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;margin:0.5rem 0}}
.qr-box{{text-align:center;margin:1rem 0}}
.qr-box img{{max-width:280px;border-radius:12px}}
.count-info{{font-size:14px;color:#64748b;margin:-0.2rem 0 0.8rem;text-align:right}}
.history-panel{{margin-top:1rem}}
.history-item{{padding:0.8rem;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:0.5rem;cursor:pointer}}
.history-item:hover{{background:#f8fafc}}
.loading{{display:inline-block;width:20px;height:20px;border:3px solid #f3f3f3;border-top:3px solid #2563eb;border-radius:50%;animation:spin 1s linear infinite;margin-right:0.5rem}}
.modal{{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;justify-content:center;align-items:center;z-index:1000}}
.modal-content{{background:white;padding:2rem;border-radius:16px;width:90%;max-width:500px}}
.close-btn{{position:absolute;top:1rem;right:1rem;font-size:24px;cursor:pointer}}
@keyframes spin {{0% {{transform: rotate(0deg);}} 100% {{transform: rotate(360deg);}}}}
@keyframes fadeIn {{from {{opacity:0}} to {{opacity:1}}}}
</style>

<div class="card">
  <div id="login_panel">
    <input id="phone" placeholder="请输入手机号" type="tel" maxlength="11" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
    <div style="display:flex;gap:0.5rem">
      <button onclick="sendCode()" id="code_btn" style="flex:1">获取验证码</button>
      <span id="code_countdown" style="display:none;line-height:2.5rem;color:#64748b">60s后重新获取</span>
    </div>
    <input id="code" placeholder="请输入6位验证码" type="tel" maxlength="6" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
    <button onclick="login()" style="width:100%">登录</button>
  </div>
  <div id="user_panel" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem">
      <p>用户：<span id="show_phone"></span></p>
      <button onclick="showHistory()" style="padding:0.4rem 0.8rem;font-size:14px">历史记录</button>
    </div>
    <p id="user_info" style="margin-bottom:0.8rem"></p>
    <div id="limit_tip" class="vip-tip" style="display:none"></div>
    <button class="btn-vip" onclick="showPay()" style="width:100%">开通会员</button>
  </div>
</div>

<!-- 支付弹窗 -->
<div id="pay_panel" class="modal" style="display:none">
  <div class="modal-content" style="position:relative">
    <span onclick="hidePay()" class="close-btn">&times;</span>
    <h3 style="margin-bottom:1rem;text-align:center">开通会员套餐</h3>
    <div style="margin-bottom:1rem">
      <button onclick="selectPackage('month')" class="btn-vip" style="width:100%;margin-bottom:0.5rem">月卡 - 19.9元（30天）</button>
      <button onclick="selectPackage('season')" class="btn-vip" style="width:100%;margin-bottom:0.5rem">季卡 - 49.9元（90天）</button>
      <button onclick="selectPackage('year')" class="btn-vip" style="width:100%">年卡 - 169.9元（365天）</button>
    </div>
    <div id="pay_qr_box" class="qr-box" style="display:none">
      <img id="qr_img" src="">
      <p id="pay_tip"></p>
    </div>
    <input id="trade_no" placeholder="输入微信支付订单后6位数字" type="tel" maxlength="6" style="display:none" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
    <button onclick="submitPay()" id="pay_submit_btn" style="width:100%;display:none">提交并开通会员</button>
  </div>
</div>

<!-- 历史记录弹窗 -->
<div id="history_modal" class="modal" style="display:none">
  <div class="modal-content" style="position:relative;max-height:80vh;overflow-y:auto">
    <span onclick="hideHistory()" class="close-btn">&times;</span>
    <h3 style="margin-bottom:1rem">使用历史</h3>
    <div id="history_list" class="history-panel"></div>
  </div>
</div>

<div class="tab">
  <button class="active" onclick="show('resume')">简历优化</button>
  <button onclick="show('copy')">文案生成</button>
</div>

<!-- 简历优化面板 -->
<div id="resume" class="panel active card">
  <select id="r_style" style="margin-bottom:0.5rem">
    <option value="STAR法则">STAR法则（突出成果）</option>
    <option value="简洁版">简洁版（适配快筛）</option>
    <option value="详细版">详细版（突出经验）</option>
    <option value="应届生版">应届生版（突出潜力）</option>
  </select>
  <textarea id="r_content" placeholder="粘贴你的原始简历内容" oninput="updateCount('r_content', 'r_count')"></textarea>
  <div class="count-info" id="r_count">0 字</div>
  <input id="r_jd" placeholder="目标岗位JD（可选，提升优化精准度）">
  <input id="r_job" value="产品经理" placeholder="目标岗位名称">
  <button onclick="goResume()" style="width:100%">优化简历</button>
  <div id="r_result" class="result"></div>
</div>

<!-- 文案生成面板 -->
<div id="copy" class="panel card">
  <select id="c_template" style="margin-bottom:0.5rem">
    <option value="通用">通用文案</option>
    <option value="求职">求职自我介绍</option>
    <option value="营销">产品营销文案</option>
    <option value="朋友圈">朋友圈文案</option>
    <option value="职场">职场沟通文案</option>
  </select>
  <input id="c_topic" placeholder="文案主题（必填）">
  <input id="c_style" value="正式" placeholder="文案风格（如：正式/活泼/幽默）">
  <input id="c_len" value="200字" placeholder="期望字数">
  <textarea id="c_context" placeholder="补充上下文/要求（可选）" oninput="updateCount('c_context', 'c_count')"></textarea>
  <div class="count-info" id="c_count">0 字</div>
  <button onclick="goCopy()" style="width:100%">生成文案</button>
  <div id="c_result" class="result"></div>
</div>

<script>
let phone = null;
let selectedPackage = "";
const qrUrl = "{WXPAY_QR_URL}";
const VIP_PACKAGES = {json.dumps(VIP_PACKAGES)};

// 切换面板
function show(id){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'))
  document.querySelectorAll('.tab button').forEach(b=>b.classList.remove('active'))
  document.getElementById(id).classList.add('active')
  event.target.classList.add('active')
}}

// 字数统计
function updateCount(inputId, countId){{
  const content = document.getElementById(inputId).value;
  document.getElementById(countId).innerText = content.length + " 字";
}}

// 复制结果
function copyResult(resultId){{
  const content = document.getElementById(resultId).innerText;
  navigator.clipboard.writeText(content).then(() => {{
    alert("内容已复制到剪贴板！");
  }}).catch(() => {{
    alert("复制失败，请手动复制");
  }});
}}

// 支付相关
function showPay(){{
  if(!phone)return alert('请先登录！');
  document.getElementById('pay_panel').style.display='flex';
  // 重置支付面板
  document.getElementById('pay_qr_box').style.display='none';
  document.getElementById('trade_no').style.display='none';
  document.getElementById('pay_submit_btn').style.display='none';
  selectedPackage = "";
}}

function hidePay(){{
  document.getElementById('pay_panel').style.display='none';
}}

function selectPackage(pkg){{
  selectedPackage = pkg;
  const price = VIP_PACKAGES[pkg].price;
  const days = VIP_PACKAGES[pkg].days;
  document.getElementById('pay_tip').innerText = `扫码支付 ${price} 元开通${days}天会员`;
  document.getElementById('pay_qr_box').style.display='block';
  document.getElementById('qr_img').src = qrUrl;
  document.getElementById('trade_no').style.display='block';
  document.getElementById('pay_submit_btn').style.display='block';
}}

// 历史记录
function showHistory(){{
  if(!phone)return alert('请先登录！');
  document.getElementById('history_modal').style.display='flex';
  loadHistory();
}}

function hideHistory(){{
  document.getElementById('history_modal').style.display='none';
}}

// 验证码倒计时
let countdownTimer = null;
function startCountdown(){{
  let count = 60;
  document.getElementById('code_btn').style.display='none';
  document.getElementById('code_countdown').style.display='inline';
  countdownTimer = setInterval(() => {{
    count--;
    document.getElementById('code_countdown').innerText = count + "s后重新获取";
    if(count <= 0){{
      clearInterval(countdownTimer);
      document.getElementById('code_btn').style.display='block';
      document.getElementById('code_countdown').style.display='none';
    }}
  }}, 1000);
}}

// API调用封装
async function fetchApi(url, options = {{}}){{
  try{{
    const resp = await fetch(url, {{
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      ...options
    }});
    return await resp.json();
  }}catch(e){{
    alert('网络错误，请重试！');
    return {{ok: false, error: '网络错误'}};
  }}
}}

// 发送验证码
async function sendCode(){{
  const p = document.getElementById('phone').value;
  if(!p || p.length !== 11)return alert('请输入有效的11位手机号！');
  const d = await fetchApi(`/api/send-code?phone=${{p}}`);
  if(d.ok){{
    alert('验证码已发送（手机短信/控制台）');
    startCountdown();
  }}else{{
    alert('发送失败：' + (d.error || '未知错误'));
  }}
}}

// 登录
async function login(){{
  const p = document.getElementById('phone').value;
  const c = document.getElementById('code').value;
  if(!p || !c)return alert('请输入手机号和验证码！');
  const d = await fetchApi(`/api/login?phone=${{p}}&code=${{c}}`);
  if(d.ok){{
    phone = p;
    document.getElementById('login_panel').style.display='none';
    document.getElementById('user_panel').style.display='block';
    document.getElementById('show_phone').innerText = p.replace(/(\\d{3})\\d{4}(\\d{4})/, '$1****$2');
    refreshUser();
  }}else{{
    alert('登录失败：' + (d.error || '验证码错误或已过期'));
  }}
}}

// 刷新用户信息
async function refreshUser(){{
  const d = await fetchApi(`/api/user-info?phone=${{phone}}`);
  if(d.vip){{
    document.getElementById('user_info').innerText = `✅ 会员有效期至：${{d.vip_expire}}`;
    document.getElementById('limit_tip').style.display='none';
  }}else{{
    document.getElementById('user_info').innerText = `今日剩余免费次数：${{d.left}}/${{d.free_limit}}`;
    document.getElementById('limit_tip').style.display = d.left < 1 ? 'block' : 'none';
    document.getElementById('limit_tip').innerText = `今日免费次数已用完，开通会员享不限次数使用！`;
  }}
}}

// 加载历史记录
async function loadHistory(){{
  const d = await fetchApi(`/api/history?phone=${{phone}}`);
  const list = document.getElementById('history_list');
  list.innerHTML = '';
  if(d.length === 0){{
    list.innerHTML = '<p style="text-align:center;color:#64748b">暂无使用记录</p>';
    return;
  }}
  d.forEach(item => {{
    const div = document.createElement('div');
    div.className = 'history-item';
    div.innerHTML = `
      <div style="font-weight:600">${{item.type === 'resume' ? '简历优化' : '文案生成'}}</div>
      <div style="font-size:14px;color:#64748b;margin:0.2rem 0">${{formatTime(item.time)}}</div>
      <div style="font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{item.input.topic || item.input.job || '无标题'}}</div>
    `;
    div.onclick = () => {{
      if(item.type === 'resume'){{
        document.getElementById('r_content').value = item.input.content || '';
        document.getElementById('r_job').value = item.input.job || '';
        document.getElementById('r_jd').value = item.input.jd || '';
        show('resume');
        hideHistory();
      }}else{{
        document.getElementById('c_topic').value = item.input.topic || '';
        document.getElementById('c_style').value = item.input.style || '';
        document.getElementById('c_len').value = item.input.len || '';
        document.getElementById('c_context').value = item.input.context || '';
        show('copy');
        hideHistory();
      }}
    }};
    list.appendChild(div);
  }});
}}

// 格式化时间
function formatTime(timestamp){{
  const date = new Date(timestamp * 1000);
  return date.toLocaleString('zh-CN');
}}

// 提交支付
async function submitPay(){{
  const code = document.getElementById('trade_no').value;
  if(!code || code.length < 4)return alert('请输入支付订单后6位数字！');
  if(!selectedPackage)return alert('请选择会员套餐！');
  const d = await fetchApi(`/api/pay-check?phone=${{phone}}&code=${{code}}&package=${{selectedPackage}}`);
  if(d.ok){{
    alert('会员开通成功！');
    hidePay();
    refreshUser();
  }}else{{
    alert('验证失败：' + (d.error || '订单号错误'));
  }}
}}

// 简历优化
async function goResume(){{
  if(!phone)return alert('请先登录！');
  await refreshUser();
  const content = document.getElementById('r_content').value;
  const job = document.getElementById('r_job').value;
  const jd = document.getElementById('r_jd').value;
  const style = document.getElementById('r_style').value;
  if(!content)return alert('请粘贴简历内容！');
  
  const btn = event.target;
  btn.disabled = true;
  btn.innerHTML = '<span class="loading"></span>生成中...';
  const d = await fetchApi('/api/resume', {{
    method: 'POST',
    body: `phone=${{phone}}&content=${{encodeURIComponent(content)}}&jd=${{encodeURIComponent(jd)}}&job=${{encodeURIComponent(job)}}&style=${{encodeURIComponent(style)}}`
  }});
  
  const resultDiv = document.getElementById('r_result');
  if(d.data){{
    resultDiv.innerHTML = `${{d.data}}<div class="copy-btn" onclick="copyResult('r_result')">复制</div>`;
  }}else{{
    resultDiv.innerText = d.error || '生成失败，请重试';
  }}
  btn.disabled = false;
  btn.innerText = '优化简历';
  await refreshUser();
}}

// 文案生成
async function goCopy(){{
  if(!phone)return alert('请先登录！');
  await refreshUser();
  const topic = document.getElementById('c_topic').value;
  const style = document.getElementById('c_style').value;
  const len = document.getElementById('c_len').value;
  const context = document.getElementById('c_context').value;
  const template = document.getElementById('c_template').value;
  if(!topic)return alert('请输入文案主题！');
  
  const btn = event.target;
  btn.disabled = true;
  btn.innerHTML = '<span class="loading"></span>生成中...';
  const d = await fetchApi('/api/copy', {{
    method: 'POST',
    body: `phone=${{phone}}&topic=${{encodeURIComponent(topic)}}&style=${{encodeURIComponent(style)}}&len=${{encodeURIComponent(len)}}&context=${{encodeURIComponent(context)}}&template=${{encodeURIComponent(template)}}`
  }});
  
  const resultDiv = document.getElementById('c_result');
  if(d.data){{
    resultDiv.innerHTML = `${{d.data}}<div class="copy-btn" onclick="copyResult('c_result')">复制</div>`;
  }}else{{
    resultDiv.innerText = d.error || '生成失败，请重试';
  }}
  btn.disabled = false;
  btn.innerText = '生成文案';
  await refreshUser();
}}
</script>
</html>
"""

# ================== API接口（升级版） ==================
@app.get("/api/send-code")
@limiter.limit("5/minute")  # 限流：每分钟最多5次
def send_code(phone: str, request: Request):
    """发送验证码（限流）"""
    if not re.match(r"^1[3-9]\d{9}$", phone):
        return {"ok": False, "error": "手机号格式错误"}
    
    # 初始化用户
    init_user(phone)
    
    # 生成6位验证码
    code = str(random.randint(100000, 999999))
    users = load_users()
    enc_phone = encrypt_phone(phone)
    
    # 更新验证码
    users[enc_phone]["code"] = code
    users[enc_phone]["code_expire"] = now() + 300  # 5分钟过期
    save_users(users)
    
    # 发送短信
    if send_sms_code(phone, code):
        return {"ok": True}
    else:
        return {"ok": False, "error": "短信发送失败"}

@app.get("/api/login")
def login(phone: str, code: str):
    """登录验证"""
    users = load_users()
    enc_phone = encrypt_phone(phone)
    u = users.get(enc_phone)
    
    if not u:
        return {"ok": False, "error": "用户不存在"}
    if u["code"] != code:
        return {"ok": False, "error": "验证码错误"}
    if now() > u["code_expire"]:
        return {"ok": False, "error": "验证码已过期"}
    
    # 清空验证码
    u["code"] = None
    u["code_expire"] = 0
    save_users(users)
    return {"ok": True}

@app.get("/api/user-info")
def user_info(phone: str):
    """获取用户信息"""
    users = load_users()
    enc_phone = encrypt_phone(phone)
    u = users.get(enc_phone)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 刷新用户状态
    check_vip_status(phone)
    
    # 计算剩余免费次数
    if u["date"] != today():
        u["date"] = today()
        u["daily_count"] = 0
        save_users(users)
    left = FREE_LIMIT - u["daily_count"] if not u["vip"] else 999
    
    # 会员到期时间格式化
    vip_expire = "永久" if u["vip"] and u.get("vip_expire", 0) > now() + 365 * 86400 else (
        format_time(u["vip_expire"]) if u.get("vip_expire", 0) > now() else "未开通"
    )
    
    return {
        "vip": u["vip"],
        "left": left,
        "free_limit": FREE_LIMIT,
        "vip_expire": vip_expire,
        "invite_code": u.get("invite_code", "")
    }

@app.get("/api/pay-check")
def pay_check(phone: str, code: str, package: str):
    """支付验证（简化版，真实场景需对接支付回调）"""
    # 真实场景：需验证微信支付订单号是否存在且金额匹配
    if len(code) < 4:
        return {"ok": False, "error": "订单号格式错误"}
    
    # 开通会员
    if activate_vip(phone, package):
        return {"ok": True}
    else:
        return {"ok": False, "error": "开通失败"}

@app.get("/api/history")
def get_user_history(phone: str):
    """获取用户历史记录"""
    history = get_history(phone)
    # 转换时间戳为格式化字符串
    for item in history:
        item["time_str"] = format_time(item["time"])
    return history

@app.post("/api/resume")
@limiter.limit("20/hour")  # 每小时最多20次请求
def api_resume(
    request: Request,
    phone: str = Form(...),
    content: str = Form(...),
    jd: str = Form(""),
    job: str = Form("产品经理"),
    style: str = Form("STAR法则")
):
    """简历优化接口（多风格）"""
    # 检查使用权限
    can_use, tip = check_use_limit(phone)
    if not can_use:
        return {"error": tip}
    
    # 过滤敏感内容
    content = filter_sensitive_content(content)
    jd = filter_sensitive_content(jd)
    
    try:
        # 根据风格生成不同的提示词
        style_prompts = {
            "STAR法则": "用STAR法则（情境-任务-行动-结果）优化，突出可量化成果和数据，语言专业简洁",
            "简洁版": "极简风格，保留核心信息，适配HR快速筛选，控制在500字以内",
            "详细版": "详细展开工作经验，突出项目细节和个人贡献，适合深度面试",
            "应届生版": "突出校园经历、实习经验和学习能力，弱化工作经验，强调潜力和可塑性"
        }
        prompt = f"""你是专业简历优化师，{style_prompts[style]}。
目标岗位：{job}
JD参考：{jd}
原始简历：{content}
要求：直接输出优化后的简历内容，无需额外说明。"""
        
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role":"user","content":prompt}]
        )
        result = resp.choices[0].message.content
        
        # 保存历史记录
        add_history(phone, "resume", {
            "content": content[:100] + "..." if len(content) > 100 else content,
            "job": job,
            "jd": jd[:50] + "..." if len(jd) > 50 else jd,
            "style": style
        }, result)
        
        return {"data": result}
    except Exception as e:
        return {"error": f"生成失败：{str(e)}"}

@app.post("/api/copy")
@limiter.limit("20/hour")
def api_copy(
    request: Request,
    phone: str = Form(...),
    topic: str = Form(...),
    style: str = Form("正式"),
    len: str = Form("200字"),
    context: str = Form(""),
    template: str = Form("通用")
):
    """文案生成接口（多模板）"""
    # 检查使用权限
    can_use, tip = check_use_limit(phone)
    if not can_use:
        return {"error": tip}
    
    # 过滤敏感内容
    topic = filter_sensitive_content(topic)
    context = filter_sensitive_content(context)
    
    try:
        # 模板化提示词
        template_prompts = {
            "通用": "通用风格文案，适配多种场景，语言流畅自然",
            "求职": "求职自我介绍文案，突出个人优势和岗位匹配度",
            "营销": "产品营销文案，突出产品卖点，有吸引力和转化力",
            "朋友圈": "朋友圈文案，轻松活泼，有感染力，适合社交传播",
            "职场": "职场沟通文案，专业得体，适配工作汇报/邮件/沟通场景"
        }
        prompt = f"""生成{template_prompts[template]}，要求如下：
主题：{topic}
风格：{style}
字数：{len}
补充说明：{context}
要求：直接输出文案正文，无需额外说明，符合所选模板风格。"""
        
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role":"user","content":prompt}]
        )
        result = resp.choices[0].message.content
        
        # 保存历史记录
        add_history(phone, "copy", {
            "topic": topic,
            "style": style,
            "len": len,
            "context": context[:100] + "..." if len(context) > 100 else context,
            "template": template
        }, result)
        
        return {"data": result}
    except Exception as e:
        return {"error": f"生成失败：{str(e)}"}

# ================== 运营后台（简单版） ==================
@app.get("/admin/stats")
def admin_stats(password: str = ""):
    """简单的运营统计（需设置密码）"""
    if password != os.getenv("ADMIN_PWD", "123456"):
        raise HTTPException(status_code=403, detail="密码错误")
    
    users = load_users()
    history = load_history()
    
    # 统计数据
    total_users = len(users)
    vip_users = sum(1 for u in users.values() if u.get("vip"))
    total_usage = sum(len(h) for h in history.values())
    today_usage = sum(1 for h_list in history.values() for h in h_list if format_time(h["time"]).split()[0] == today())
    
    return {
        "total_users": total_users,
        "vip_users": vip_users,
        "vip_rate": f"{vip_users/total_users*100:.1f}%" if total_users > 0 else "0%",
        "total_usage": total_usage,
        "today_usage": today_usage
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)