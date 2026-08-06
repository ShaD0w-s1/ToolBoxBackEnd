#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

def main():
    # Local development is the safe default. CloudBase explicitly selects
    # cloudrun.settings_scf from scf_bootstrap and cloudrun/wsgi.py.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cloudrun.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
