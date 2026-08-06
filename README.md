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
python -m pip install -r requirements.txt
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
GET, POST            /api/projects/
GET, PATCH, DELETE   /api/projects/<project_id>/
GET, PUT             /api/templates/A320/
GET, PUT             /api/templates/B787/
GET, PUT             /api/tool-cart/
GET                  /api/csrf/
```

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
