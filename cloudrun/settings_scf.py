"""CloudBase HTTP 云函数专用的最小化生产设置。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 正式部署脚本会生成并长期保存稳定密钥，避免重部署后会话全部失效。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "cloudbase-django-http-function-demo-key")
DEBUG = False
# 外部域名由 CloudBase 网关管理；应用层不绑定某一个临时网关域名。
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "cloudrun.urls"
WSGI_APPLICATION = "cloudrun.wsgi.application"
# 生产 API 不依赖 Django ORM、管理后台或本地会话，减少启动面和故障面。
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
INSTALLED_APPS = ["api"]
# 压缩中间件放在最外层：响应阶段它最后执行，压缩后的 Content-Length 才不会被
# CommonMiddleware 覆盖。云函数本身不压缩（实测网关仅在 header 声明
# Vary: Accept-Encoding，即使客户端请求 br 也不压缩），必须由应用层处理。
MIDDLEWARE = [
    "api.compression.ResponseCompressionMiddleware",
    "django.middleware.common.CommonMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
