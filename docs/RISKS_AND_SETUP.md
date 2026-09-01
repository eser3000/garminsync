# 已知风险与联调说明

## 1. garth 版本与 MFA 实现（重要）
- 已确认 garth **0.4.47** 的真实 API 与早期文档不同：
  - `login()` 为**阻塞式**，无 `return_on_mfa` 参数；MFA 通过 `prompt_mfa` 回调获取验证码。
  - 无原生 `resume_login` / `save` / `resume`；Token 持久化改用 `client.dumps()` / `client.loads()`（base64 字符串）。
  - 下载/上传用原生 `client.download(path)` / `client.upload(fp)`。
- 本项目据此实现：后端在**后台线程**运行 `client.login(..., prompt_mfa=<队列回调>)`，
  MFA 验证码经由 `queue.Queue` 从前端传入，登录线程阻塞等待直到取码完成。
- Garmin 认证流程改版后**新账户登录可能失效**。若中国区登录报认证错误：
  - 关注 garth 仓库 issue 中 Garmin 认证变化；
  - 必要时替换 `app/services/garmin.py` 中的登录实现（下载/上传基于 Connect API，可保留）。

## 2. 联调步骤（需真实双账户）
1. `docker compose up -d`，访问 `http://<飞牛NAS的IP>:1234`（bridge + 端口映射）。
2. 首次打开设置管理员用户名 + 密码（至少 8 位）；之后每次访问输入用户名密码登录。
3. 中国区：填邮箱/密码 → 触发短信 → 填验证码完成登录（若 5 分钟内未收到码可重登）。
4. 国际区：填邮箱/密码完成登录（若也要求 MFA，复用同一验证码弹窗）。
5. 设置频率（如 60 分钟）并启用。
6. 点"立即同步"验证一次，观察日志状态 success/partial/error。

## 3. 数据位置
- SQLite：`/data/app.db`（docker-compose 挂载 `./data:/data`）。
- Garmin token：以 base64 形式存于 `accounts.token` 列（DB 内），不再依赖外部 token 文件。

## 4. 同步行为说明
- 仅同步中国区账户中"尚未同步过"的活动（按 activityId 去重）。
- 每个活动独立下载(FIT)并上传国际区；单个失败不影响其余（记为 partial）。
- 上传目标为对应国际区账户，活动会作为新活动出现。

## 5. 频率档位
`[10, 15, 30, 60, 120, 180, 240, 360]` 分钟（10 分钟 ~ 6 小时）。
后端对非法值返回 400。

## 6. 本地依赖安装（国内镜像）
若默认 PyPI 下载慢，使用清华镜像：
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```
