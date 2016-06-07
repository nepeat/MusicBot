import decimal
from musicbot.config import Config, ConfigDefaults
from musicbot.permissions import Permissions, PermissionsDefaults
import hashlib
import aiohttp
import itertools
import bisect
import random


def sane_round_int(x):
    return int(decimal.Decimal(x).quantize(1, rounding=decimal.ROUND_HALF_UP))


def load_config(context):
    context.config = Config(ConfigDefaults.options_file)
    context.permissions = Permissions(PermissionsDefaults.perms_file, grant_all=[context.config.owner_id])


def md5sum(filename, limit=0):
    fhash = hashlib.md5()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            fhash.update(chunk)
    return fhash.hexdigest()[-limit:]


def weighted_choice(items):
    if isinstance(items, dict):
        choices, weights = zip(*items.items())
    else:
        choices, weights = zip(*items)

    # Ensure that the weights are integrers if they are bytes or strings.
    weights = [int(x) for x in weights]

    cumdist = list(itertools.accumulate(weights))
    choice = random.random() * cumdist[-1]

    return choices[bisect.bisect(cumdist, choice)]


def migrate_redis(redis):
    # Horribly duct tapey migrations but they work.
    if redis.type("musicbot:played") == "set":
        items = redis.smembers("musicbot:played")
        redis.delete("musicbot:played")
        redis.hmset("musicbot:played", {key: 1 for key in items})


async def get_header(session, url, headerfield=None, *, timeout=5):
    with aiohttp.Timeout(timeout):
        async with session.head(url) as response:
            if headerfield:
                return response.headers.get(headerfield)
            else:
                return response.headers
