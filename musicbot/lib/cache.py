import inspect
import os

from dogpile.cache import make_region


def make_key_generator(namespace, fn, value_mangler=str, arg_blacklist=(
    'self',
    'cls',
    'bot',
    'loop',
    'retry_on_error',
    'on_error',
)):
    """
    Create a cache key generator for function calls of fn.
    :param namespace:
        Value to prefix all keys with. Useful for differentiating methods with
        the same name but in different classes.
    :param fn:
        Function to create a key generator for.
    :param value_mangler:
        Each value passed to the function is run through this mangler.
        Default: str
    :param arg_blacklist:
        Iterable of arguments to ignore when creating a key.
    Returns a function which can be called with the same arguments as fn but
    returns a corresponding key for that call.
    Note: Ingores fn(..., *arg, **kw) parameters.

    Code taken from https://gist.github.com/shazow/6838337
    """
    # TODO: Include parent class in name?
    # TODO: Better default value_mangler?
    fn_args = inspect.getfullargspec(fn).args
    if not fn_args:
        fn_args = []

    arg_blacklist = arg_blacklist or []

    if namespace is None:
        namespace = '%s:%s' % (fn.__module__, fn.__name__)
    else:
        namespace = '%s:%s|%s' % (fn.__module__, fn.__name__, namespace)

    def generate_key(*arg, **kw):
        if not kw:
            kw = {}

        kw.update(zip(fn_args, arg))

        for arg in arg_blacklist:
            kw.pop(arg, None)

        key = namespace + '|' + ' '.join(value_mangler(kw[k]) for k in sorted(kw))
        return key

    return generate_key

redis_config = {
    'host': [os.environ.get("REDIS_HOST", "localhost")],
    'port': int(os.environ.get("REDIS_PORT", 6379)),
    'db': '1',
    'redis_expiration_time': 60 * 60 * 24 * 7,  # 1 week
    'distributed_lock': True
}

downloader = make_region(
    function_key_generator=make_key_generator,
    key_mangler=lambda key: "musicbot:cache:" + key,
).configure(
    'dogpile.cache.redis',
    arguments=redis_config,
)
