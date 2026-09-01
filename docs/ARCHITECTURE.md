# GarminSync — 佳明中国区数据自动同步工具

## 1. 项目目标
部署在私人 NAS（Docker）上的网页应用：
- 免登录，打开网页直接使用（仅内网）
- 配置两个佳明账户：
  - **源账户（中国区）**：`connect.garmin.cn`，登录支持短信二次验证（MFA）
  - **目标账户（国际区）**：`connect.garmin.com`
- 登录后按页面设置的频率（10 分钟 ~ 6 小时，可选档位）自动定时将中国区活动数据同步到国际区
- 提供同步状态、日志、手动触发

## 2. 技术栈
- 后端：Python 3.11 + FastAPI + Uvicorn
- 佳明授权/同步：`garth`（参考其登录与 connectapi 方式）
- 存储：SQLite（文件，单文件便于 NAS 备份）
- 前端：原生 HTML/CSS/JS（无构建步骤，FastAPI 直接托管 templates/static）
- 调度：后台 asyncio 循环（按频率触发，无额外依赖）
- 容器：Docker + docker-compose

## 3. 目录结构（唯一事实来源）
```
app/
  main.py            # FastAPI 实例、路由挂载、静态/模板托管、启动调度器
  config.py          # 从环境变量读配置
  db.py              # SQLite 引擎 + 会话工厂
  models.py          # SQLAlchemy ORM 模型
  schemas.py         # Pydantic 请求/响应模型
  services/
    garmin.py        # Garth 集成：登录、MFA、下载、上传、token 存取
    sync.py          # 同步编排：取中国区活动→下载→上传国际区→去重
    scheduler.py     # 后台调度循环
  api/
    accounts.py      # 中国区/国际区 登录、MFA、登出、状态
    settings.py      # 频率设置读写
    sync.py          # 手动触发、状态、日志
  templates/index.html
  static/css/style.css
  static/js/app.js
docs/
  PROJECT_PLAN.md    # PM 维护：里程碑/进度
  ARCHITECTURE.md    # 本文件（契约）
docker-compose.yml
Dockerfile
pyproject.toml
requirements.txt
.env.example
```

## 4. 数据模型（SQLAlchemy）
```python
class Account(Base):
    __tablename__ = "accounts"
    region      = Column(String, primary_key=True)  # 'china' | 'intl'
    email       = Column(String)
    username    = Column(String)
    token       = Column(Text)   # client.dumps() 的 base64 字符串（含 OAuth1/OAuth2）
    logged_in   = Column(Boolean, default=False)
    mfa_pending = Column(Boolean, default=False)
    updated_at  = Column(DateTime)

class Setting(Base):
    __tablename__ = "settings"
    id               = Column(Integer, primary_key=True)
    frequency_minutes= Column(Integer, default=60)  # 10..360
    enabled          = Column(Boolean, default=False)
    updated_at       = Column(DateTime)

class SyncLog(Base):
    __tablename__ = "sync_log"
    id          = Column(Integer, primary_key=True)
    started_at  = Column(DateTime)
    finished_at = Column(DateTime)
    status      = Column(String)   # running|success|partial|error
    synced_count= Column(Integer, default=0)
    error       = Column(Text)

class SyncedActivity(Base):
    __tablename__ = "synced_activities"
    id             = Column(Integer, primary_key=True)
    china_activity_id = Column(String, unique=True)
    intl_activity_id   = Column(String)
    synced_at         = Column(DateTime)
```

频率合法档位（前端下拉 + 后端校验）：`[10, 15, 30, 60, 120, 180, 240, 360]`（分钟）。

## 5. REST API 契约（前端与后端必须严格一致）
所有 `/api/*` 及 `/` 页面**无需登录鉴权**，打开网页直接使用。

### 5.1 中国区账户（短信 MFA）
- `POST /api/accounts/china/login`
  body: `{email, password}`
  返回：`{mfa_required: true, mfa_token: "<region>"}` 或 `{mfa_required: false, username}` 或 `{error: "..."}`
  说明：后端在**后台线程**中运行 garth 的阻塞式 `client.login(..., prompt_mfa=<队列回调>)`。
  若登录在等待短信验证码，接口先返回 `mfa_required=true`；稍后前端提交验证码唤醒线程。
- `POST /api/accounts/china/mfa`
  body: `{mfa_token, code}`（mfa_token 即 region 字符串，如 `'china'`）
  将验证码投入队列，轮询至 `Account.logged_in` 为真，返回 `{ok:true, username}` 或 `{ok:false, error}`。
- `POST /api/accounts/intl/login` / `intl/mfa`：同中国区（国际区若也要求 MFA 复用同一流程）。
- `POST /api/accounts/logout`  body `{region:'china'|'intl'}` → 清除该账户 token
- `GET /api/accounts/status` → `{china:{logged_in,username,email}, intl:{...}}`

### 5.3 设置
- `GET /api/settings` → `{frequency_minutes, enabled, options:[10,15,30,60,120,180,240,360]}`
- `POST /api/settings` body `{frequency_minutes, enabled}` → 校验档位，保存，返回新值

### 5.4 同步
- `POST /api/sync/now` → 立即触发一次同步（异步），返回 `{ok:true, triggered:true}`
- `GET /api/sync/status` → `{last_sync:{started_at,finished_at,status,synced_count}, next_run_at, enabled, frequency_minutes}`
- `GET /api/sync/log?limit=20` → `[SyncLog...]`

## 6. Garth 集成要点（services/garmin.py，已按 garth 0.4.47 真实源码确认）
```python
from garth import Client

# 中国区（domain 决定区域）
cn = Client(domain="garmin.cn")

# 阻塞式登录；MFA 通过 prompt_mfa 回调获取验证码（本项目用 Queue 在后台线程间传码）
cn.login(email, password, client=cn, prompt_mfa=lambda: code)

# 国际区
intl = Client(domain="garmin.com")
intl.login(email, password, client=intl, prompt_mfa=lambda: code)

# 读取活动列表（JSON）
acts = cn.connectapi("/activitylist-service/activities", params={"start":0,"limit":30})

# 下载活动文件（FIT 二进制）—— garth 原生方法，已带鉴权
fit_bytes = cn.download(f"/download-service/files/activity/{activity_id}")

# 上传到国际区（garth 原生 upload，接收文件对象）
result = intl.upload(fp)  # fp 需有 .name 属性
intl_id = result["detailedImportResult"]["successes"][0]["activityId"]

# Token 持久化：用 client.dumps()/loads()（base64 字符串），不依赖内部 token 结构
token_b64 = cn.dumps()
cn2 = Client(domain="garmin.cn"); cn2.loads(token_b64)  # 恢复会话
```
- 中国区登录为抓取的前提（必须）；国际区为可选：未登录时仍会从中国区抓取（计入 `fetched_count`），但跳过上传（`status=partial`，原因注明"国际区未登录，已抓取 N 条但未上传"）。两端均登录才完整执行「抓取 + 上传」。
- MFA 两步式由后台线程 + `queue.Queue` 实现：登录线程阻塞在 `prompt_mfa`，前端提交验证码后唤醒。

## 7. 调度（services/scheduler.py）
- 应用启动时启动 asyncio 任务，每 30 秒检查一次：
  - 若 `settings.enabled` 且 距上次同步 >= `frequency_minutes` 且 中国区已登录 → 执行 sync.run()
  - 维护 `next_run_at`（内存计算）

## 8. 同步编排（services/sync.py）
1. 取中国区最近 N 个活动（按时间倒序，仅取未超过窗口的）
2. 过滤已在 `synced_activities` 的 activity_id
3. 对每个：下载 FIT → 上传国际区 → 记录 `SyncedActivity`
4. 写 `SyncLog`（status=success/partial/error）
5. 任一活动失败不阻断其余（partial）

## 9. 已知风险（必须向用户披露）
- Garth 官方文档标注已弃用，Garmin 认证改版后**新账户登录可能失效**。本工具按 Garth 当前 API 实现，如登录失败需关注 Garmin 认证变化。
- 佳明中国区活动上传到国际区是否受 Garmin 政策限制未验证。
- 实时联调需真实双账户凭证，自动化测试对非网络逻辑做覆盖。
