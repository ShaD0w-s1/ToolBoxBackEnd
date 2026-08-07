# 定检工具清单后端

基于 Django 的 HTTP API，业务数据存储在腾讯云 CloudBase NoSQL。服务采用普通短请求，不使用 WebSocket。

## 技术边界

- Django ORM：仅用于本地 SQLite、会话以及后续用户/微信鉴权数据。
- CloudBase NoSQL：存储工作项目、机型标准库和工具车数据。
- 前端：允许 Vue 3 开发服务器从 `localhost:5173` 调用。
- 云端环境：`da-tool-list-d2g0awsejc0658949`（上海）。

## 本地开发

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python manage.py migrate
python manage.py runserver
```

服务地址：<http://127.0.0.1:8000/>。

## 环境变量

复制 `.env.example` 为 `.env`，并填写 CloudBase 服务端 API Key：

```dotenv
DJANGO_SECRET_KEY=replace-me
CLOUDBASE_ENV_ID=da-tool-list-d2g0awsejc0658949
CLOUDBASE_API_KEY=your-server-api-key
CLOUDBASE_NOSQL_INSTANCE=(default)
CLOUDBASE_NOSQL_DATABASE=(default)
```

`.env` 已被 Git 忽略。API Key 不得放入前端、提交到 Git 或发送到聊天中。

## REST API

```text
GET                  /api/cloudbase/status/
GET                  /api/poll/
GET, POST            /api/projects/
GET, PATCH, DELETE   /api/projects/<project_id>/
GET, PUT             /api/templates/A320/
GET, PUT             /api/templates/B787/
GET, PUT             /api/tool-cart/
GET                  /api/csrf/
```

`GET /api/poll/` 用于客户端的 5 秒变更探测。首次请求不带参数取得
`revision`，后续通过 `?revision=<revision>` 轮询；`changed=true` 时客户端再
重新读取业务数据。修订值直接从三个业务集合的完整当前状态计算，不依赖云
函数内存或额外通知记录，因此实例冷启动、重建以及文档删除都不会漏报。

浏览器执行写操作前，应先请求 `/api/csrf/`，随后将 `csrftoken` Cookie 的值通过 `X-CSRFToken` 请求头传回。

项目文档示例：

```json
{
  "name": "A320 定检",
  "aircraft_type": "A320",
  "team": "A1",
  "use_tool_cart": false,
  "sections": [
    {
      "name": "A",
      "note": "前舱",
      "tasks": [
        {
          "name": "工作 A1",
          "items": [
            {"name": "扳手", "quantity": 2}
          ]
        }
      ]
    }
  ]
}
```

## 检查

```powershell
python manage.py check
python manage.py test
```

CloudBase 部署显式使用 `cloudrun.settings_scf`；本地运行默认使用 `cloudrun.settings`。

## Git 与环境分离

- `main`：生产分支，只有该分支允许触发 CloudBase 部署。
- `develop`：日常开发与本地验证分支，不直接部署生产环境。
- `.env`：本地配置，不进入 Git。
- CloudBase 函数环境变量：生产配置，保存在云端，不进入 Git。

发布流程：

```text
develop 开发与测试 → 合并到 main → 推送 main → 部署 CloudBase
```

Git 仓库只保存无密钥的代码和配置模板；API Key、微信密钥等始终通过本地或云端环境变量管理。

## 生产部署脚本

生产部署只读取 `main` 中由 `deploy/production-files.txt` 明确列出的运行文件。它不会上传 `.env`、SQLite、测试、本地设置、文档或虚拟环境。

先执行只读预检：

```powershell
.\scripts\deploy-production.ps1
```

确认预检输出后部署到普通 HTTP 云函数 `toolbox-api`：

```powershell
.\scripts\deploy-production.ps1 -Deploy
```

首次使用新名称部署时，脚本允许把原来指向 `dtlapi` 的 `/api` 路由切换到
`toolbox-api`。旧函数会保留作为人工回退点，不会被部署脚本自动删除。

## GitHub Actions

`.github/workflows/ci-cd.yml` 会在 `main`、`develop` 的推送和 PR 上执行 Django
检查与测试；只有 `main` 推送或从 `main` 手动运行时才会部署生产 API。请在 GitHub
仓库的 `production` Environment 中配置以下 Secrets：

- `TCB_SECRET_ID`：腾讯云 CAM SecretId
- `TCB_SECRET_KEY`：腾讯云 CAM SecretKey
- `TCB_ENV_ID`：CloudBase 环境 ID
- `CLOUDBASE_API_KEY`：后端访问 CloudBase NoSQL 的服务端 API Key
- `DJANGO_PRODUCTION_SECRET_KEY`：稳定的 Django 生产密钥，后续不要更换

可选配置 Environment Variable `CSRF_TRUSTED_ORIGINS`，值为逗号分隔的 HTTPS
Origin；不配置时继续使用脚本内现有的生产域名。建议给 `production` Environment
启用 required reviewers，并为 `main` 启用分支保护，要求 CI 通过后才能合并。

脚本会自动完成：

1. 从 `main` 生成确定性的生产包；
2. 检查 CloudBase MCP 登录环境；
3. 创建或更新 Python 3.10 HTTP 函数；
4. 合并云端环境变量并设置 60 秒超时；
5. 创建或校验公开的 `/api` HTTP 网关路由；
6. 等待函数进入 `Active/Available`；
7. 通过线上 `/api/projects/` 读取 CloudBase NoSQL，完成冒烟测试。

首次正式部署会在被 Git 忽略的 `.env` 中生成稳定的 `DJANGO_PRODUCTION_SECRET_KEY`，后续部署不会使现有 Django 会话失效。
