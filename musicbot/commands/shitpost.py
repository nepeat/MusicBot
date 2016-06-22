import functools
import billboard
import aiohttp
import json

from musicbot.commands import command
from musicbot.commands.music import cmd_play
from musicbot.structures import Response
from musicbot.utils import weighted_choice

from concurrent.futures import ThreadPoolExecutor

thread_pool = ThreadPoolExecutor(max_workers=2)


def cache_billboard(redis):
    if redis.exists("musicbot:chart:billboard"):
        return

    chart = billboard.ChartData('hot-100')
    for song in chart:
        redis.sadd("musicbot:chart", "%s %s" % (song.title, song.artist))

    redis.setex("musicbot:chart:billboard", 86400, "1")

async def cache_soundcloud(redis, session):
    if redis.exists("musicbot:chart:soundcloud"):
        return

    with aiohttp.Timeout(10):
        async with session.get("https://api-v2.soundcloud.com/charts?kind=top&genre=soundcloud:genres:all-music&client_id=02gUJC0hH2ct1EGOcYXQIzRFU91c72Ea&limit=20&offset=0&linked_partitioning=1") as response:
            try:
                parsed = json.loads(response)
            except (TypeError, ValueError):
                return

            for song in parsed.get("collection", []):
                track = song.get("track", {})

                if "permalink_url" not in track or not track["permalink_url"]:
                    continue

                redis.sadd("musicbot:chart", track["permalink_url"])

    redis.setex("musicbot:chart:soundcloud", 86400, "1")

async def get_random_top(bot, redis):
    await bot.loop.run_in_executor(thread_pool, functools.partial(cache_billboard, redis))
    await cache_soundcloud(redis, bot.aiosession)

    return redis.srandmember("musicbot:chart")


@command("surprise")
async def cmd_surprise(self, player, channel, author, permissions, redis, mode="fun"):

    if mode == "serious":
        url = await get_random_top(self, redis)
    else:
        urls = redis.hgetall("musicbot:played")
        url = weighted_choice(urls)

    if url:
        return await cmd_play(self, player, channel, author, permissions, None, url)
    else:
        return Response(
            "There are no songs that can be played. Play a few songs and try this command again.",
            delete_after=25
        )
