# 佳明中国区数据自动同步工具 — 部署与管理

## 功能
- 活动同步：中国区 → 国际区（FIT 转发），可选 → iGPSPORT 迹驰 App
- 体重同步：小米体脂秤 → 佳明双区（固定每天 00:00）
- 健康数据同步：佳明中国区 ⇄ 国际区全量对比、互相补全（固定每天 03:00）

iGPSPORT 同步说明：独立的上传模块（不修改原活动同步逻辑）。基于其 App 内部
接口（OAuth 登录 + OSS 直传三段式 `getSignedUrl → PUT OSS → uploadByOss`），
非官方开放 API，个人自用等价于官方 App 手动上传；接口可能随 App 版本变动。
在网页「⑦ iGPSPORT 迹驰同步」卡片登录迹驰账号即启用，随活动同步一并执行，
同步频率与数据范围直接复用佳明活动同步的设置，无需单独配置。

## 环境变量（.env）
| 变量 | 说明 | 默认 |
| --- | --- | --- |
| PORT | 容器监听端口 | 1234 |
| DATA_DIR | SQLite 等数据目录 | /data |
| TZ | 时区 | Asia/Shanghai |

## 启动（docker-compose）
```bash
docker compose up -d
# 访问 http://<nas-ip>:1234 即可直接使用（免登录，仅内网使用）
```

## 数据持久化
- 挂载 `./data:/data`，SQLite 存于 `/data/app.db`

## 端口映射
- `1234:1234`

## 构建镜像
```bash
docker compose build
```

## 本地开发（不依赖 Docker）
```bash
python3 -m venv .venv
. .venv/bin/activate
# 国内镜像（清华）加速：
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
export DATA_DIR=./data
python -m uvicorn app.main:app --host 0.0.0.0 --port 1234
```

## 运行测试
```bash
. .venv/bin/activate
python -m pytest -q
```

## 已知风险
- garth 已不再积极维护，Garmin 认证改版后新账户登录可能失效（详见 `docs/RISKS_AND_SETUP.md`）。
- 中国区活动上传至国际区属跨区同步，请遵守 Garmin 服务条款；上传基于 Connect 公开 API，下载/上传逻辑可独立于登录实现保留。
- MFA 验证码有等待窗口（默认 300 秒，见 `MFA_WAIT_SECONDS`），超时需重新登录。
- iGPSPORT 同步走其 App 内部接口（非官方开放 API），随 App 更新可能变动失效；token 过期会自动用 refresh_token 刷新，刷新失败需在网页重新登录。

