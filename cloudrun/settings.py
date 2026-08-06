"""仅供本地开发使用的 Django 设置。

CloudBase 始终显式使用 ``cloudrun.settings_scf``。两套设置分离，可以防止
本地 SQLite、调试模式和开发跨域配置意外进入生产环境。
"""

from dotenv import load_dotenv

from .settings_scf import *  # noqa: F403


# .env 被 Git 忽略，只在本地加载；云端配置来自函数环境变量。
load_dotenv(BASE_DIR / ".env")  # noqa: F405


DEBUG = True
SECRET_KEY = "django-local-development-only"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

DATABASES = {
    # SQLite 仅供 Django 框架自身使用，不存放项目、模板或工具车业务数据。
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
    }
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "corsheaders",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

CORS_ALLOWED_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
CORS_ALLOW_CREDENTIALS = True

STATIC_URL = "static/"
