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
INSTALLED_APPS = ["api"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
