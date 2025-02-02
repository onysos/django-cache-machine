from __future__ import unicode_literals

import django

__all__ = ['DEFAULT_TIMEOUT', 'FOREVER']


if django.VERSION[:2] >= (1, 6):
    from django.core.cache.backends.base import DEFAULT_TIMEOUT as DJANGO_DEFAULT_TIMEOUT
    DEFAULT_TIMEOUT = 300 if django.VERSION[:2] == (1, 6) else DJANGO_DEFAULT_TIMEOUT
    FOREVER = None
else:
    DEFAULT_TIMEOUT = None
    FOREVER = 0
