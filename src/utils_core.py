"""
Generic utility functions that are called frequently across modules.
"""
import functools
import typing
from datetime import datetime, timezone

import tensorflow as tf

from .dataclass import ModelParameter

_NAME_INDICES = {}
tf1 = tf.compat.v1


def reset_scope():
    _NAME_INDICES.clear()


def scoped(name: str, fn: typing.Callable, *args, **kwargs):
    name = random_name(name)
    with tf1.variable_scope(f'{name}v', reuse=tf.compat.v1.AUTO_REUSE), tf1.name_scope(f'{name}n'):
        return fn(*args, **kwargs)


def default(value: typing.Any, default_value: typing.Any) -> typing.Any:
    """
    Return a default value if a given value is None.
    This is merely a comfort function to avoid typing out "x if x is None else y" over and over again.
    :param value: value that can be None
    :param default_value: default if value is None
    :return: value or default_value
    """
    return default_value if value is None else value


def chunks(lst: typing.List, n: int):
    """
    Yield successive n-sized chunks from lst.
    :param lst: the list to be split.
    :param n: the chunk size.
    """
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def timestamp():
    return "{}".format(datetime.now(timezone.utc).isoformat())


def color_print(params: ModelParameter, string):
    print(f"{params.own_color}{timestamp()} {string}{params.other_color}", flush=True)


def int_reduce_mul(*integers: typing.Union[typing.List[typing.Iterable[int]], typing.List[int]]) -> int:
    if isinstance(integers[0], typing.Iterable):
        integers = integers[0]
    return functools.reduce(int.__mul__, integers)


def random_name(prefix="") -> str:
    """
    Generates a random name based on the globally set seed using python's random module.
    Each name has 256 bits of entropy and a final length of 44 base64 encoded characters.
    For the sake of convenience, special characters are removed from the final string.
    :return: random string
    """
    if prefix not in _NAME_INDICES:
        _NAME_INDICES[prefix] = -1
    _NAME_INDICES[prefix] += 1
    return f'{prefix}{_NAME_INDICES[prefix]}'
