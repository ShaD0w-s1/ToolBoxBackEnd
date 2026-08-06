#!/usr/bin/env python
"""Django 管理命令入口。"""
import os
import sys

def main():
    # 默认使用安全的本地开发设置；CloudBase 会由 scf_bootstrap 和 WSGI
    # 入口显式选择 cloudrun.settings_scf。
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cloudrun.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
