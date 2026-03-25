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
import re
import redis
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
# 注释：无需短信验证，删除requests依赖（用于短信API调用）

# ================== 基础配置 ==================
# 关键校验：确保FastAPI入口实例正确，与vercel.json的entrypoint: main:app完全匹配
app = FastAPI(title="AI简历·文案SaaS Pro")  # 实例名固定为app，无拼写错误、无多余修饰

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
WXPAY_QR_URL = "https://s41.ax1x.com/2026/03/23/peKuPxg.jpg"  # 替换为自己的收款码URL

# 初始化 Redis 客户端（适配 Upstash Redis，替代下架的Vercel KV，完全兼容原逻辑）
redis_client = redis.from_url(
    os.getenv("REDIS_URL"),
    decode_responses=True,  # 自动将bytes转为str，避免手动解码
    ssl=True  # Upstash Redis 强制使用SSL连接，固定开启
)

# OpenAI客户端（添加超时，适配Vercel函数限制）
client = OpenAI(
    api_key=DOUBAO_API_KEY,
    base_url="https://ark.cn-beijing.volces.com/api/v3"
)
MODEL_NAME = "doubao-2.0-lite"

# ================== 工具函数 ==================
def encrypt_phone(phone: str) -> str:
    """手机号加密存储（方案1、2用到，方案3可删除）"""
    return hashlib.md5(phone.encode("utf-8")).hexdigest()[:16]

def today() -> str:
    return str(date.today())

def now() -> int:
    """当前时间戳"""
    return int(time.time())

def format_time(timestamp: int) -> str:
    """时间戳转格式化字符串"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")

def filter_sensitive_content(content: str) -> str:
    """过滤敏感内容"""
    sensitive_words = ["暴力", "色情", "赌博"]  # 可扩展敏感词库
    for word in sensitive_words:
        content = content.replace(word, "*" * len(word))
    return content

# ================== 用户数据操作（Upstash Redis 替代 Vercel KV，逻辑不变） ==================
def init_user(identifier: str) -> dict:
    """初始化用户数据（Redis Hash 存储），适配3种登录方案，identifier为登录标识（手机号/邮箱/用户名）"""
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        # 初始化用户数据
        user_data = {
            "identifier": identifier,  # 存储登录标识（手机号/邮箱/用户名）
            "code": None,  # 验证码（方案1、2用到）
            "code_expire": 0,  # 验证码过期时间（方案1、2用到）
            "vip": "False",  # Redis 存储字符串，用"True"/"False"表示布尔值
            "vip_expire": 0,  # 会员到期时间戳
            "vip_package": "",  # 会员套餐类型
            "date": today(),
            "daily_count": 0,
            "invite_code": f"INV{random.randint(100000, 999999)}",  # 邀请码
            "invited_by": ""  # 被谁邀请
        }
        redis_client.hset(user_key, mapping=user_data)
    # 返回用户数据
    return redis_client.hgetall(user_key)

def check_vip_status(identifier: str) -> bool:
    """检查会员是否有效，identifier为登录标识（手机号/邮箱/用户名）"""
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        return False
    # 读取会员到期时间
    vip_expire = int(redis_client.hget(user_key, "vip_expire"))
    # 会员未过期则标记为VIP，否则取消
    if vip_expire > now():
        redis_client.hset(user_key, "vip", "True")
        return True
    else:
        redis_client.hset(user_key, "vip", "False")
        return False

def check_use_limit(identifier: str) -> tuple[bool, str]:
    """检查使用限制（返回：是否可用，提示信息），identifier为登录标识（手机号/邮箱/用户名）"""
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        return False, "用户未登录"
    
    # 会员直接放行
    if check_vip_status(identifier):
        return True, "会员不限次数"
    
    # 非会员检查每日免费次数
    current_date = today()
    user_date = redis_client.hget(user_key, "date")
    if user_date != current_date:
        redis_client.hset(user_key, mapping={"date": current_date, "daily_count": 0})
    
    daily_count = int(redis_client.hget(user_key, "daily_count"))
    if daily_count >= FREE_LIMIT:
        return False, f"今日免费次数已用完（{FREE_LIMIT}次），开通会员继续使用"
    
    # 增加当日使用次数
    redis_client.hincrby(user_key, "daily_count", 1)
    return True, f"今日剩余免费次数：{FREE_LIMIT - (daily_count + 1)}"

def activate_vip(identifier: str, package: str) -> bool:
    """开通会员套餐，identifier为登录标识（手机号/邮箱/用户名）"""
    if package not in VIP_PACKAGES:
        return False
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        return False
    
    # 计算到期时间
    days = VIP_PACKAGES[package]["days"]
    current_expire = int(redis_client.hget(user_key, "vip_expire"))
    new_expire = now() + days * 86400 if current_expire < now() else current_expire + days * 86400
    
    # 更新会员信息
    redis_client.hset(user_key, mapping={
        "vip": "True",
        "vip_expire": new_expire,
        "vip_package": package
    })
    return True

def add_history(identifier: str, type_: str, input_data: dict, output: str):
    """添加使用历史（Redis List 存储，保留最近50条），identifier为登录标识（手机号/邮箱/用户名）"""
    history_key = f"history:{encrypt_phone(identifier)}" if identifier.isdigit() else f"history:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    # 构造历史记录
    history_item = json.dumps({
        "id": f"H{now()}{random.randint(100, 999)}",
        "type": type_,  # resume/copy
        "input": input_data,
        "output": output,
        "time": now()
    })
    # 插入到列表头部，保留最近50条
    redis_client.lpush(history_key, history_item)
    redis_client.ltrim(history_key, 0, 49)  # 只保留前50条

def get_history(identifier: str) -> list:
    """获取用户历史记录，identifier为登录标识（手机号/邮箱/用户名）"""
    history_key = f"history:{encrypt_phone(identifier)}" if identifier.isdigit() else f"history:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    # 获取所有历史记录，转为字典列表
    history_list = redis_client.lrange(history_key, 0, -1)
    history_data = []
    for item in history_list:
        item_dict = json.loads(item)
        item_dict["time_str"] = format_time(item_dict["time"])
        history_data.append(item_dict)
    return history_data

# ================== 无短信验证：3种登录方案（已启用方案1，其余注释） ==================
# 方案1：本地验证码（无需第三方，直接在前端显示，适合测试/轻量使用）【已启用】
@app.get("/api/get-code")
@limiter.limit("5/minute")  # 限流：每分钟最多5次，防止恶意获取验证码
def get_code(identifier: str, request: Request):
    """获取本地验证码（无需短信，返回验证码给前端显示），identifier为手机号/邮箱/用户名"""
    # 验证标识格式（可根据需求调整，如仅允许手机号/邮箱）
    if identifier.isdigit() and not re.match(r"^1[3-9]\d{9}$", identifier):
        return {"ok": False, "error": "手机号格式错误"}
    if "@" in identifier and not re.match(r"^[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)+$", identifier):
        return {"ok": False, "error": "邮箱格式错误"}
    
    # 初始化用户（首次获取验证码自动创建用户）
    init_user(identifier)
    
    # 生成6位验证码（纯数字，便于用户输入）
    code = str(random.randint(100000, 999999))
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    
    # 更新验证码（5分钟过期，避免长期有效）
    redis_client.hset(user_key, mapping={
        "code": code,
        "code_expire": str(now() + 300)
    })
    
    # 返回验证码给前端（前端显示，用户手动输入）
    return {"ok": True, "code": code, "msg": "验证码已返回前端，请手动输入（5分钟内有效）"}

# 方案2：邮箱验证码（需配置SMTP，适合正式使用）【未启用，如需使用请取消注释并配置】
# def send_email_code(identifier: str, code: str) -> bool:
#     """发送邮箱验证码（需配置SMTP）"""
#     import smtplib
#     from email.mime.text import MIMEText
#     from email.header import Header
#     # 配置SMTP（替换为自己的邮箱信息）
#     smtp_server = "smtp.163.com"
#     smtp_port = 465
#     smtp_user = "你的邮箱@163.com"
#     smtp_pwd = "你的邮箱授权码"  # 不是登录密码，需在邮箱设置中开启SMTP授权
#     # 构造邮件内容
#     msg = MIMEText(f"你的AI简历·文案SaaS验证码为：{code}，5分钟内有效，请勿泄露给他人。", "plain", "utf-8")
#     msg["From"] = Header("AI简历SaaS", "utf-8")
#     msg["To"] = Header(identifier, "utf-8")
#     msg["Subject"] = Header("验证码验证", "utf-8")
#     # 发送邮件
#     try:
#         with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
#             server.login(smtp_user, smtp_pwd)
#             server.sendmail(smtp_user, [identifier], msg.as_string())
#         return True
#     except Exception as e:
#         print(f"邮箱验证码发送失败：{str(e)}")
#         return False

# @app.get("/api/send-email-code")
# @limiter.limit("5/minute")
# def send_email_code_api(identifier: str, request: Request):
#     """发送邮箱验证码接口（方案2），identifier为用户邮箱"""
#     if not re.match(r"^[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)+$", identifier):
#         return {"ok": False, "error": "邮箱格式错误"}
#     init_user(identifier)
#     code = str(random.randint(100000, 999999))
#     user_key = f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
#     redis_client.hset(user_key, mapping={"code": code, "code_expire": str(now() + 300)})
#     if send_email_code(identifier, code):
#         return {"ok": True, "msg": "验证码已发送至你的邮箱（5分钟内有效）"}
#     else:
#         return {"ok": False, "error": "邮箱验证码发送失败，请检查SMTP配置"}

# 方案3：免验证登录（无需验证码，直接登录，适合测试/快速部署）【未启用，如需使用请取消注释】
# @app.get("/api/no-auth-login")
# def no_auth_login(identifier: str):
#     """免验证登录，identifier为手机号/邮箱/用户名（直接创建/登录用户）"""
#     if len(identifier)< 4:
#         return {"ok": False, "error": "登录标识长度不能少于4位"}
#     init_user(identifier)
#     return {"ok": True, "msg": "登录成功，可正常使用所有功能"}

# ================== 通用登录验证接口（适配方案1、2，方案3无需此接口） ==================
@app.get("/api/login")
def login(identifier: str, code: str):
    """登录验证（适配本地验证码、邮箱验证码），identifier为登录标识（手机号/邮箱/用户名）"""
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        return {"ok": False, "error": "用户不存在，请先获取验证码"}
    
    # 验证验证码和过期时间
    code_store = redis_client.hget(user_key, "code")
    code_expire = int(redis_client.hget(user_key, "code_expire"))
    
    if code_store != code:
        return {"ok": False, "error": "验证码错误，请重新输入"}
    if now() > code_expire:
        return {"ok": False, "error": "验证码已过期，请重新获取"}
    
    # 登录成功：清空验证码（防止重复使用）
    redis_client.hset(user_key, mapping={"code": None, "code_expire": 0})
    return {"ok": True, "msg": "登录成功，可正常使用所有功能"}

# ================== 核心业务接口（补充缺失接口，确保功能完整） ==================
# 简历优化接口
@app.post("/api/resume")
@limiter.limit("10/minute")
async def generate_resume(
    request: Request,
    identifier: str = Form(...),
    content: str = Form(...),
    job: str = Form(...),
    jd: str = Form(default=""),
    style: str = Form(default="STAR法则")
):
    """简历优化接口，适配前端调用"""
    # 检查使用权限
    can_use, msg = check_use_limit(identifier)
    if not can_use:
        return {"ok": False, "error": msg}
    
    # 过滤敏感内容
    content = filter_sensitive_content(content)
    jd = filter_sensitive_content(jd)
    
    # 构造AI提示词
    prompt = f"""请按照{style}风格，结合目标岗位「{job}」和JD「{jd}」，优化以下简历内容。
要求：突出核心能力和工作成果，语言专业简洁，适配目标岗位需求，修正语法错误，排版清晰，保留原始简历关键信息，避免冗余。
原始简历：{content}"""
    
    try:
        # 调用AI生成优化简历
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是专业的简历优化专家，擅长根据不同岗位和风格优化简历，突出个人亮点和竞争力。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=15.0  # 适配Vercel函数超时限制
        )
        result = response.choices[0].message.content
        # 记录使用历史
        add_history(identifier, "resume", {"content": content, "job": job, "jd": jd, "style": style}, result)
        return {"ok": True, "data": result}
    except Exception as e:
        return {"ok": False, "error": f"简历优化失败：{str(e)}"}

# 文案生成接口
@app.post("/api/copy")
@limiter.limit("10/minute")
async def generate_copy(
    request: Request,
    identifier: str = Form(...),
    topic: str = Form(...),
    style: str = Form(default="正式"),
    len: str = Form(default="300字"),
    template: str = Form(default="通用"),
    context: str = Form(default="")
):
    """文案生成接口，适配前端调用"""
    # 检查使用权限
    can_use, msg = check_use_limit(identifier)
    if not can_use:
        return {"ok": False, "error": msg}
    
    # 过滤敏感内容
    topic = filter_sensitive_content(topic)
    context = filter_sensitive_content(context)
    
    # 构造AI提示词
    prompt = f"""请按照「{style}」风格、「{len}」篇幅、「{template}」模板，结合补充说明「{context}」，围绕主题「{topic}」生成文案。
要求：语言流畅，贴合模板风格，符合篇幅要求，突出主题，无冗余内容，适合目标场景使用。"""
    
    try:
        # 调用AI生成文案
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是专业的文案创作大师，擅长各类风格、各类模板的文案生成，贴合用户需求，语言有感染力。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=15.0
        )
        result = response.choices[0].message.content
        # 记录使用历史
        add_history(identifier, "copy", {"topic": topic, "style": style, "len": len, "template": template, "context": context}, result)
        return {"ok": True, "data": result}
    except Exception as e:
        return {"ok": False, "error": f"文案生成失败：{str(e)}"}

# 用户信息接口（适配前端显示）
@app.get("/api/user-info")
def get_user_info(identifier: str):
    """获取用户信息，适配前端显示"""
    user_key = f"user:{encrypt_phone(identifier)}" if identifier.isdigit() else f"user:{hashlib.md5(identifier.encode('utf-8')).hexdigest()[:16]}"
    if not redis_client.exists(user_key):
        return {"ok": False, "error": "用户不存在"}
    
    user_data = redis_client.hgetall(user_key)
    # 处理数据格式，适配前端显示
    return {
        "ok": True,
        "data": {
            "identifier": user_data["identifier"],
            "vip": user_data["vip"] == "True",
            "vip_expire": format_time(int(user_data["vip_expire"])) if int(user_data["vip_expire"]) > now() else "已过期",
            "vip_package": user_data["vip_package"] or "未开通会员",
            "free_limit": FREE_LIMIT,
            "left_count": FREE_LIMIT - int(user_data["daily_count"]) if user_data["vip"] == "False" else "不限次数",
            "invite_code": user_data["invite_code"]
        }
    }

# 会员开通接口（模拟支付，实际需对接支付平台）
@app.get("/api/activate-vip")
def activate_vip_api(identifier: str, package: str, order_code: str):
    """会员开通接口，order_code为模拟订单号（实际需对接支付平台验证）"""
    # 简单验证订单号格式（实际需对接微信/支付宝支付接口）
    if not order_code.startswith(ORDER_PREFIX):
        return {"ok": False, "error": "订单号格式错误"}
    
    # 激活会员
    if activate_vip(identifier, package):
        return {"ok": True, "msg": f"会员开通成功，有效期{VIP_PACKAGES[package]['days']}天"}
    else:
        return {"ok": False, "error": "会员开通失败，请重试"}

# 历史记录接口
@app.get("/api/history")
def get_user_history(identifier: str):
    """获取用户使用历史，适配前端显示"""
    history_data = get_history(identifier)
    return {"ok": True, "data": history_data}

# ================== 前端页面（完整版，适配所有接口，修复缺失内容） ==================
@app.get("/", response_class=HTMLResponse)
def index():
    """前端页面入口，适配所有功能接口，修复缺失的HTML内容和JS逻辑"""
    return f"""<!DOCTYPE html>
AI简历·文案SaaS ProAI简历·文案SaaS Pro<!-- 登录区域（适配方案1：本地验证码） -->
        用户登录（本地验证码）<!-- VIP开通区域 -->
        开通会员，不限次数使用
                    月卡：19.9元/30天  季卡：49.9元/90天                     年卡：169.9元/365天 <!-- 功能标签页 -->
        <!-- 简历优化 -->
        简历优化（多风格可选）优化结果：<!-- 文案生成 -->
       文案生成（多模板可选）生成结果：<!-- 使用历史 -->
        使用历史暂无使用记录<!-- 用户信息 -->
       用户信息请先登录类型：${item.type === 'resume' ? '简历优化' : '文案生成'}时间：${item.time_str}标题：${item.input.topic || item.input.job || '无标题'}内容：${item.output.substring(0, 50)}... 
                                    <button class="copy-btn" onclick="copyText('${item.output.replace(/'/g, "\复制结果登录标识：${userData.identifier}会员状态：${userData.vip ? '已开通' : '未开通'}会员到期时间：${userData.vip_expire}会员套餐：${userData.vip_package}每日免费次数：${userData.free_limit}次今日剩余次数：${userData.left_count}邀请码：${userData.invite_code}"""
except Exception as e:
    return HTMLResponse(content="前端页面加载失败，请刷新重试", status_code=500)
