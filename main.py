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
WXPAY_QR_URL = "https://s41.ax1x.com/2026/03/23/peKuPxg.jpg"  # 替换为自己的收款码URL
SMS_API_KEY = os.getenv("SMS_API_KEY", "")  # 短信平台API Key（如阿里云/腾讯云）

# 初始化 Redis 客户端（适配 Upstash Redis，替代下架的Vercel KV，完全兼容原逻辑）
# Upstash Redis 开通后，会自动生成 REDIS_URL 环境变量，无需手动配置host、port、password
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

# ================== 用户数据操作（Upstash Redis 替代 Vercel KV，逻辑不变） ==================
def init_user(phone: str) -> dict:
    """初始化用户数据（Redis Hash 存储）"""
    user_key = f"user:{encrypt_phone(phone)}"
    if not redis_client.exists(user_key):
        # 初始化用户数据
        user_data = {
            "phone": phone,  # 可改为仅存加密后手机号（更安全）
            "code": None,
            "code_expire": 0,
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

def check_vip_status(phone: str) -> bool:
    """检查会员是否有效"""
    user_key = f"user:{encrypt_phone(phone)}"
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

def check_use_limit(phone: str) -> tuple[bool, str]:
    """检查使用限制（返回：是否可用，提示信息）"""
    user_key = f"user:{encrypt_phone(phone)}"
    if not redis_client.exists(user_key):
        return False, "用户未登录"
    
    # 会员直接放行
    if check_vip_status(phone):
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

def activate_vip(phone: str, package: str) -> bool:
    """开通会员套餐"""
    if package not in VIP_PACKAGES:
        return False
    user_key = f"user:{encrypt_phone(phone)}"
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

def add_history(phone: str, type_: str, input_data: dict, output: str):
    """添加使用历史（Redis List 存储，保留最近50条）"""
    history_key = f"history:{encrypt_phone(phone)}"
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

def get_history(phone: str) -> list:
    """获取用户历史记录"""
    history_key = f"history:{encrypt_phone(phone)}"
    # 获取所有历史记录，转为字典列表
    history_list = redis_client.lrange(history_key, 0, -1)
    return [json.loads(item) for item in history_list]

# ================== 前端页面（升级版） ==================
@app.get("/", response_class=HTMLResponse)
def index():
    return f"""<!DOCTYPE html>
AI简历·文案SaaS Pro60s后重新获取用户：&times;开通会员套餐&times;使用历史0 字0 字暂无使用记录${{item.type === 'resume' ? '简历优化' : '文案生成'}}${{formatTime(item.time)}}${{item.input.topic || item.input.job || '无标题'}}复制复制
"""

# ================== API接口（升级版，适配Upstash Redis） ==================
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
    user_key = f"user:{encrypt_phone(phone)}"
    
    # 更新验证码
    redis_client.hset(user_key, mapping={
        "code": code,
        "code_expire": str(now() + 300)  # 5分钟过期
    })
    
    # 发送短信
    if send_sms_code(phone, code):
        return {"ok": True}
    else:
        return {"ok": False, "error": "短信发送失败"}

@app.get("/api/login")
def login(phone: str, code: str):
    """登录验证"""
    user_key = f"user:{encrypt_phone(phone)}"
    if not redis_client.exists(user_key):
        return {"ok": False, "error": "用户不存在"}
    
    code_store = redis_client.hget(user_key, "code")
    code_expire = int(redis_client.hget(user_key, "code_expire"))
    
    if code_store != code:
        return {"ok": False, "error": "验证码错误"}
    if now() > code_expire:
        return {"ok": False, "error": "验证码已过期"}
    
    # 清空验证码
    redis_client.hset(user_key, mapping={"code": None, "code_expire": 0})
    return {"ok": True}

@app.get("/api/user-info")
def user_info(phone: str):
    """获取用户信息"""
    user_key = f"user:{encrypt_phone(phone)}"
    if not redis_client.exists(user_key):
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 刷新用户状态
    check_vip_status(phone)
    
    # 计算剩余免费次数
    current_date = today()
    user_date = redis_client.hget(user_key, "date")
    if user_date != current_date:
        redis_client.hset(user_key, mapping={"date": current_date, "daily_count": 0})
    
    daily_count = int(redis_client.hget(user_key, "daily_count"))
    vip = redis_client.hget(user_key, "vip") == "True"
    left = FREE_LIMIT - daily_count if not vip else 999
    
    # 会员到期时间格式化
    vip_expire = int(redis_client.hget(user_key, "vip_expire"))
    vip_expire_str = "永久" if vip and vip_expire > now() + 365 * 86400 else (
        format_time(vip_expire) if vip_expire > now() else "未开通"
    )
    
    return {
        "vip": vip,
        "left": left,
        "free_limit": FREE_LIMIT,
        "vip_expire": vip_expire_str,
        "invite_code": redis_client.hget(user_key, "invite_code")
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
            messages=[{"role":"user","content":prompt}],
            timeout=8.0  # 适配Vercel函数10秒超时限制
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
            messages=[{"role":"user","content":prompt}],
            timeout=8.0  # 适配Vercel函数10秒超时限制
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
    
    # 统计用户数（Redis 模糊匹配 user:* 键）
    user_keys = redis_client.keys("user:*")
    total_users = len(user_keys)
    
    # 统计会员数
    vip_users = 0
    for key in user_keys:
        if redis_client.hget(key, "vip") == "True":
            vip_users += 1
    
    # 统计总使用次数（所有用户历史记录总和）
    history_keys = redis_client.keys("history:*")
    total_usage = 0
    today_usage = 0
    today_str = today()
    for key in history_keys:
        history_list = redis_client.lrange(key, 0, -1)
        total_usage += len(history_list)
        # 统计今日使用次数
        for item_str in history_list:
            item = json.loads(item_str)
            item_date = format_time(item["time"]).split()[0]
            if item_date == today_str:
                today_usage += 1
    
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
