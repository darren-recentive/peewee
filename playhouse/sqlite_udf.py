import hashlib
import heapq
import math
import os
import random
import sys
import threading
import zlib
try:
    from collections import Counter
except ImportError:
    Counter = None
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from peewee import binary_construct
from peewee import unicode_type
try:
    from playhouse._speedups import format_date_time_sqlite
except ImportError:
    from peewee import format_date_time
    from peewee import SQLITE_DATETIME_FORMATS

    def format_date_time_sqlite(date_value):
        return format_date_time(date_value, SQLITE_DATETIME_FORMATS)

try:
    from playhouse import _sqlite_udf as cython_udf
except ImportError:
    cython_udf = None


# Group udf by function.
CONTROL_FLOW = 'control_flow'
DATE = 'date'
FILE = 'file'
HELPER = 'helpers'
MATH = 'math'
STRING = 'string'

AGGREGATE_COLLECTION = {}
UDF_COLLECTION = {}


class synchronized_dict(dict):
    def __init__(self, *args, **kwargs):
        super(synchronized_dict, self).__init__(*args, **kwargs)
        self._lock = threading.Lock()

    def __getitem__(self, key):
        with self._lock:
            return super(synchronized_dict, self).__getitem__(key)

    def __setitem__(self, key, value):
        with self._lock:
            return super(synchronized_dict, self).__setitem__(key, value)

    def __delitem__(self, key):
        with self._lock:
            return super(synchronized_dict, self).__delitem__(key)


STATE = synchronized_dict()
SETTINGS = synchronized_dict()

# Class and function decorators.
def aggregate(*groups):
    def decorator(klass):
        for group in groups:
            AGGREGATE_COLLECTION.setdefault(group, [])
            AGGREGATE_COLLECTION[group].append(klass)
        return klass
    return decorator

def udf(*groups):
    def decorator(fn):
        for group in groups:
            UDF_COLLECTION.setdefault(group, [])
            UDF_COLLECTION[group].append(fn)
        return fn
    return decorator

# Register aggregates / functions with connection.
def register_aggregate_groups(conn, *groups):
    seen = set()
    for group in groups:
        klasses = AGGREGATE_COLLECTION[group]
        for klass in klasses:
            name = getattr(klass, 'name', klass.__name__)
            if name not in seen:
                seen.add(name)
                conn.create_aggregate(name, -1, klass)

def register_udf_groups(conn, *groups):
    seen = set()
    for group in groups:
        functions = UDF_COLLECTION[group]
        for function in functions:
            name = function.__name__
            if name not in seen:
                seen.add(name)
                conn.create_function(name, -1, function)

def register_all(conn):
    register_aggregate_groups(conn, *AGGREGATE_COLLECTION)
    register_udf_groups(conn, *UDF_COLLECTION)


# Begin actual user-defined functions and aggregates.

# Scalar functions.
@udf(CONTROL_FLOW)
def if_then_else(cond, truthy, falsey=None):
    if cond:
        return truthy
    return falsey

@udf(DATE)
def strip_tz(date_str):
    date_str = date_str.replace('T', ' ')
    tz_idx1 = date_str.find('+')
    if tz_idx1 != -1:
        return date_str[:tz_idx1]
    tz_idx2 = date_str.find('-')
    if tz_idx2 > 13:
        return date_str[:tz_idx2]
    return date_str

@udf(DATE)
def human_delta(nseconds, glue=', '):
    parts = (
        (86400 * 365, 'year'),
        (86400 * 30, 'month'),
        (86400 * 7, 'week'),
        (86400, 'day'),
        (3600, 'hour'),
        (60, 'minute'),
        (1, 'second'),
    )
    accum = []
    for offset, name in parts:
        val, nseconds = divmod(nseconds, offset)
        if val:
            suffix = val != 1 and 's' or ''
            accum.append('%s %s%s' % (val, name, suffix))
    if not accum:
        return '0 seconds'
    return glue.join(accum)

@udf(FILE)
def file_ext(filename):
    try:
        res = os.path.splitext(filename)
    except ValueError:
        return None
    return res[1]

@udf(FILE)
def file_read(filename):
    try:
        with open(filename) as fh:
            return fh.read()
    except:
        pass

if sys.version_info[0] == 2:
    @udf(HELPER)
    def gzip(data, compression=9):
        return binary_construct(zlib.compress(data, compression))

    @udf(HELPER)
    def gunzip(data):
        return zlib.decompress(data)
else:
    @udf(HELPER)
    def gzip(data, compression=9):
        return zlib.compress(binary_construct(data), compression)

    @udf(HELPER)
    def gunzip(data):
        return zlib.decompress(data).decode('utf-8')

@udf(HELPER)
def hostname(url):
    parse_result = urlparse(url)
    if parse_result:
        return parse_result.netloc

@udf(HELPER)
def toggle(key, on=None):
    key = key.lower()
    if on is not None:
        STATE[key] = on
    else:
        STATE[key] = on = not STATE.get(key)
    return on

@udf(HELPER)
def setting(key, *args):
    if not args:
        return SETTINGS.get(key)
    elif len(args) == 1:
        SETTINGS[key] = args[0]
    else:
        return False

@udf(HELPER)
def clear_settings():
    SETTINGS.clear()

@udf(HELPER)
def clear_toggles():
    STATE.clear()

@udf(MATH)
def randomrange(start, end=None, step=None):
    if end is None:
        start, end = 0, start
    elif step is None:
        step = 1
    return random.randrange(start, end, step)

@udf(MATH)
def gauss_distribution(mean, sigma):
    try:
        return random.gauss(mean, sigma)
    except ValueError:
        return None

@udf(MATH)
def sqrt(n):
    try:
        return math.sqrt(n)
    except ValueError:
        return None

@udf(MATH)
def tonumber(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except:
            return None

@udf(STRING)
def substr_count(haystack, needle):
    if not haystack or not needle:
        return 0
    return haystack.count(needle)

@udf(STRING)
def strip_chars(haystack, chars):
    return unicode_type(haystack).strip(chars)

def _hash(constructor, *args):
    hash_obj = constructor()
    for arg in args:
        hash_obj.update(arg)
    return hash_obj.hexdigest()

@udf(STRING)
def md5(*vals):
    return _hash(hashlib.md5)

@udf(STRING)
def sha1(*vals):
    return _hash(hashlib.sha1)

@udf(STRING)
def sha256(*vals):
    return _hash(hashlib.sha256)

@udf(STRING)
def sha512(*vals):
    return _hash(hashlib.sha512)

@udf(STRING)
def adler32(s):
    return zlib.adler32(s)

@udf(STRING)
def crc32(s):
    return zlib.crc32(s)

# Aggregates.
class _heap_agg(object):
    def __init__(self):
        self.heap = []
        self.ct = 0

    def process(self, value):
        return value

    def step(self, value):
        self.ct += 1
        heapq.heappush(self.heap, self.process(value))

class _datetime_heap_agg(_heap_agg):
    def process(self, value):
        return format_date_time_sqlite(value)

@aggregate(DATE)
class mintdiff(_datetime_heap_agg):
    def finalize(self):
        dtp = min_diff = None
        while self.heap:
            if min_diff is None:
                if dtp is None:
                    dtp = heapq.heappop(self.heap)
                    continue
            dt = heapq.heappop(self.heap)
            diff = dt - dtp
            if min_diff is None or min_diff > diff:
                min_diff = diff
            dtp = dt
        if min_diff is not None:
            return min_diff.total_seconds()

@aggregate(DATE)
class avgtdiff(_datetime_heap_agg):
    def finalize(self):
        if self.ct < 1:
            return
        elif self.ct == 1:
            return 0

        total_seconds = ct = 0
        dtp = None
        while self.heap:
            if total_seconds == 0:
                if dtp is None:
                    dtp = heapq.heappop(self.heap)
                    continue

            dt = heapq.heappop(self.heap)
            diff = dt - dtp
            ct += 1
            total_seconds += diff.total_seconds()
            dtp = dt

        return float(total_seconds) / ct

@aggregate(DATE)
class duration(object):
    def __init__(self):
        self._min = self._max = None

    def step(self, value):
        dt = format_date_time_sqlite(value)
        if self._min is None or dt < self._min:
            self._min = dt
        if self._max is None or dt > self._max:
            self._max = dt

    def finalize(self):
        if self._min and self._max:
            return (self._max - self._min).total_seconds()
        return None

@aggregate(MATH)
class mode(object):
    if Counter:
        def __init__(self):
            self.items = Counter()

        def step(self, *args):
            self.items.update(args)

        def finalize(self):
            if self.items:
                return self.items.most_common(1)[0][0]
    else:
        def __init__(self):
            self.items = []

        def step(self, item):
            self.items.append(item)

        def finalize(self):
            if self.items:
                return max(set(self.items), key=items.count)

@aggregate(MATH)
class minrange(_heap_agg):
    def finalize(self):
        if self.ct == 0:
            return
        elif self.ct == 1:
            return 0

        prev = min_diff = None

        while self.heap:
            if min_diff is None:
                if prev is None:
                    prev = heapq.heappop(self.heap)
                    continue
            curr = heapq.heappop(self.heap)
            diff = curr - prev
            if min_diff is None or min_diff > diff:
                min_diff = diff
            prev = curr
        return min_diff

@aggregate(MATH)
class avgrange(_heap_agg):
    def finalize(self):
        if self.ct == 0:
            return
        elif self.ct == 1:
            return 0

        total = ct = 0
        prev = None
        while self.heap:
            if total == 0:
                if prev is None:
                    prev = heapq.heappop(self.heap)
                    continue

            curr = heapq.heappop(self.heap)
            diff = curr - prev
            ct += 1
            total += diff
            prev = curr

        return float(total) / ct

@aggregate(MATH)
class _range(object):
    name = 'range'

    def __init__(self):
        self._min = self._max = None

    def step(self, value):
        if self._min is None or value < self._min:
            self._min = value
        if self._max is None or value > self._max:
            self._max = value

    def finalize(self):
        if self._min is not None and self._max is not None:
            return self._max - self._min
        return None


if cython_udf is not None:
    damerau_levenshtein_dist = udf(STRING)(cython_udf.damerau_levenshtein_dist)
    levenshtein_dist = udf(STRING)(cython_udf.levenshtein_dist)
    str_dist = udf(STRING)(cython_udf.str_dist)
    median = aggregate(MATH)(cython_udf.median)
