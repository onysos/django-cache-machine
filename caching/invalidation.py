from __future__ import unicode_literals

import collections
import functools
import hashlib
import logging
import socket

import django
from django.conf import settings
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import InvalidCacheBackendError
from django.utils import encoding, translation, six
from django.utils.six.moves.urllib.parse import parse_qsl

try:
    import redis as redislib
except ImportError:
    redislib = None

# Look for an own cache first before falling back to the default cache
try:
    if django.VERSION[:2] >= (1, 7):
        from django.core.cache import caches
        cache = caches['cache_machine']
    else:
        from django.core.cache import get_cache
        cache = get_cache('cache_machine')
except (InvalidCacheBackendError, ValueError):
    cache = default_cache

from caching import config

log = logging.getLogger('caching.invalidation')


def make_key(k, with_locale=True):
    """Generate the full key for ``k``, with a prefix."""
    key = encoding.smart_bytes('%s:%s' % (config.CACHE_PREFIX, k))
    if with_locale:
        key += encoding.smart_bytes(translation.get_language())
    # memcached keys must be < 250 bytes and w/o whitespace, but it's nice
    # to see the keys when using locmem.
    return hashlib.md5(key).hexdigest()


def flush_key(obj):
    """We put flush lists in the flush: namespace."""
    key = obj if isinstance(obj, six.string_types) else obj.cache_key
    return config.FLUSH + make_key(key, with_locale=False)


def byid(obj):
    key = obj if isinstance(obj, six.string_types) else obj.cache_key
    return make_key('byid:' + key)


def safe_redis(return_type):
    """
    Decorator to catch and log any redis errors.

    return_type (optionally a callable) will be returned if there is an error.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kw):
            try:
                return f(*args, **kw)
            except (socket.error, redislib.RedisError) as e:
                log.error('redis error: %s' % e)
                # log.error('%r\n%r : %r' % (f.__name__, args[1:], kw))
                if hasattr(return_type, '__call__'):
                    return return_type()
                else:
                    return return_type
        return wrapper
    return decorator


class Invalidator(object):

    def invalidate_keys(self, keys):
        """Invalidate all the flush lists named by the list of ``keys``."""
        if not keys:
            return
        obj_keys, flush_keys = self.find_flush_lists(keys)
        if obj_keys:
            log.debug('obj_keys: %s' % obj_keys)
            cache.delete_many(obj_keys)
        if flush_keys:
            log.debug('flush_keys: %s' % flush_keys)
            self.clear_flush_lists(flush_keys)

    def cache_objects(self, model, objects, query_key, query_flush):
        # Add this query to the flush list of each object.  We include
        # query_flush so that other things can be cached against the queryset
        # and still participate in invalidation.
        flush_keys = [o.flush_key() for o in objects]

        flush_lists = collections.defaultdict(set)
        for key in flush_keys:
            log.debug('adding %s to %s' % (query_flush, key))
            flush_lists[key].add(query_flush)
        flush_lists[query_flush].add(query_key)
        # Add this query to the flush key for the entire model, if enabled
        model_flush = flush_key(model.model_key())
        if config.CACHE_INVALIDATE_ON_CREATE == config.WHOLE_MODEL:
            flush_lists[model_flush].add(query_key)
        # Add each object to the flush lists of its foreign keys.
        for obj in objects:
            obj_flush = obj.flush_key()
            for key in map(flush_key, obj._cache_keys()):
                if key not in (obj_flush, model_flush):
                    log.debug('related: adding %s to %s' % (obj_flush, key))
                    flush_lists[key].add(obj_flush)
                if config.FETCH_BY_ID:
                    flush_lists[key].add(byid(obj))
        self.add_to_flush_list(flush_lists)

    def find_flush_lists(self, keys):
        """
        Recursively search for flush lists and objects to invalidate.

        The search starts with the lists in `keys` and expands to any flush
        lists found therein.  Returns ({objects to flush}, {flush keys found}).
        """
        objs = set(keys)
        search_keys = keys = set(map(flush_key, keys))

        # Add other flush keys from the lists, which happens when a parent
        # object includes a foreign key.
        while 1:
            new_keys = set()
            for key in self.get_flush_lists(search_keys):
                if key.startswith(config.FLUSH):
                    new_keys.add(key)
                else:
                    objs.add(key)
            if new_keys:
                log.debug('search for %s found keys %s' % (search_keys, new_keys))
                keys.update(new_keys)
                search_keys = new_keys
            else:
                return objs, keys

    def add_to_flush_list(self, mapping):
        """Update flush lists with the {flush_key: [query_key,...]} map."""
        flush_lists = collections.defaultdict(set)
        flush_lists.update(cache.get_many(list(mapping.keys())))
        for key, list_ in list(mapping.items()):
            if flush_lists[key] is None:
                flush_lists[key] = set(list_)
            else:
                flush_lists[key].update(list_)
        cache.set_many(flush_lists)

    def get_flush_lists(self, keys):
        """Return a set of object keys from the lists in `keys`."""
        return set(e for flush_list in
                   [_f for _f in list(cache.get_many(keys).values()) if _f]
                   for e in flush_list)

    def clear_flush_lists(self, keys):
        """Remove the given keys from the database."""
        cache.delete_many(keys)


class RedisInvalidator(Invalidator):

    def safe_key(self, key):
        if ' ' in key or '\n' in key:
            log.warning('BAD KEY: "%s"' % key)
            return ''
        return key

    @safe_redis(None)
    def add_to_flush_list(self, mapping):
        """Update flush lists with the {flush_key: [query_key,...]} map."""
        pipe = redis.pipeline(transaction=False)
        for key, list_ in list(mapping.items()):
            for query_key in list_:
                # Redis happily accepts unicode, but returns byte strings,
                # so manually encode and decode the keys on the flush list here
                pipe.sadd(self.safe_key(key), query_key.encode('utf-8'))
        pipe.execute()

    @safe_redis(set)
    def get_flush_lists(self, keys):
        flush_list = redis.sunion(list(map(self.safe_key, keys)))
        return [k.decode('utf-8') for k in flush_list]

    @safe_redis(None)
    def clear_flush_lists(self, keys):
        redis.delete(*list(map(self.safe_key, keys)))


class NullInvalidator(Invalidator):

    def add_to_flush_list(self, mapping):
        return


def parse_backend_uri(backend_uri):
    """
    Converts the "backend_uri" into a host and any extra params that are
    required for the backend. Returns a (host, params) tuple.
    """
    backend_uri_sliced = backend_uri.split('://')
    if len(backend_uri_sliced) > 2:
        raise InvalidCacheBackendError(
            "Backend URI can't have more than one scheme://")
    elif len(backend_uri_sliced) == 2:
        rest = backend_uri_sliced[1]
    else:
        rest = backend_uri_sliced[0]

    host = rest
    qpos = rest.find('?')
    if qpos != -1:
        params = dict(parse_qsl(rest[qpos+1:]))
        host = rest[:qpos]
    else:
        params = {}
    if host.endswith('/'):
        host = host[:-1]

    return host, params


def get_redis_backend():
    """Connect to redis from a string like CACHE_BACKEND."""
    # From django-redis-cache.
    server, params = parse_backend_uri(settings.REDIS_BACKEND)
    db = params.pop('db', 0)
    try:
        db = int(db)
    except (ValueError, TypeError):
        db = 0
    try:
        socket_timeout = float(params.pop('socket_timeout'))
    except (KeyError, ValueError):
        socket_timeout = None
    password = params.pop('password', None)
    if ':' in server:
        host, port = server.split(':')
        try:
            port = int(port)
        except (ValueError, TypeError):
            port = 6379
    else:
        host = 'localhost'
        port = 6379
    return redislib.Redis(host=host, port=port, db=db, password=password,
                          socket_timeout=socket_timeout)


if config.CACHE_MACHINE_NO_INVALIDATION:
    invalidator = NullInvalidator()
elif config.CACHE_MACHINE_USE_REDIS:
    redis = get_redis_backend()
    invalidator = RedisInvalidator()
else:
    invalidator = Invalidator()
