import sys
import django

__all__ = ['DEFAULT_TIMEOUT', 'FOREVER', 'u', 'basestring_']


if django.VERSION[:2] >= (1, 6):
    from django.core.cache.backends.base import DEFAULT_TIMEOUT as DJANGO_DEFAULT_TIMEOUT
    DEFAULT_TIMEOUT = DJANGO_DEFAULT_TIMEOUT
    FOREVER = None
else:
    DEFAULT_TIMEOUT = None
    FOREVER = 0

if sys.version_info < (3,):
    import codecs

    def u(x):
        return codecs.unicode_escape_decode(x)[0]
    basestring_ = basestring  # flake8: noqa
else:
    def u(x):
        return x
    basestring_ = str
