import functools
import json
import os

import redis
import youtube_dl

import asyncio
from concurrent.futures import ThreadPoolExecutor
from musicbot.connections import redis_pool

ytdl_format_options = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

# Fuck your useless bugreports message that gets two link embeds and confuses users
youtube_dl.utils.bug_reports_message = lambda: ''

'''
    Alright, here's the problem.  To catch youtube-dl errors for their useful information, I have to
    catch the exceptions with `ignoreerrors` off.  To not break when ytdl hits a dumb video
    (rental videos, etc), I have to have `ignoreerrors` on.  I can change these whenever, but with async
    that's bad.  So I need multiple ytdl objects.

'''

class Downloader:
    def __init__(self, download_folder=None):
        self.redis = redis.StrictRedis(connection_pool=redis_pool)
        self.thread_pool = ThreadPoolExecutor(max_workers=4)
        self.unsafe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl.params['ignoreerrors'] = True
        self.download_folder = download_folder

        if download_folder:
            otmpl = self.unsafe_ytdl.params['outtmpl']
            self.unsafe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)
            # print("setting template to " + os.path.join(download_folder, otmpl))

            otmpl = self.safe_ytdl.params['outtmpl']
            self.safe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)

    @property
    def ytdl(self):
        return self.safe_ytdl

    def set_cache(self, url, data, **kwargs):
        cachekey = "musicbot:cache:" + url

        # Do not cache searches on YouTube.
        if "url" in data and data["url"].startswith("ytsearch"):
            return None

        if "process" in kwargs and kwargs["process"] is True:
            cachekey += ":processed"

        self.redis.setex(cachekey, 60 * 60 * 24 * 7, json.dumps(data))

    def get_cache(self, url, **kwargs):
        cachekey = "musicbot:cache:" + url

        # Don't hit the cache if we are downloading the video.
        if "download" in kwargs and kwargs["download"] is True:
            return None

        if "process" in kwargs and kwargs["process"] is True:
            cachekey += ":processed"

        if not self.redis.exists(cachekey):
            return None

        try:
            return json.loads(self.redis.get(cachekey))
        except json.JSONDecodeError:
            return None

    async def extract_info(self, loop, *args, on_error=None, retry_on_error=False, **kwargs):
        """
            Runs ytdl.extract_info within the threadpool. Returns a future that will fire when it's done.
            If `on_error` is passed and an exception is raised, the exception will be caught and passed to
            on_error as an argument.
        """

        info = await loop.run_in_executor(self.thread_pool, functools.partial(self.get_cache, args[0], **kwargs))
        if info:
            return info

        if callable(on_error):
            try:
                info = await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))
                loop.run_in_executor(self.thread_pool, functools.partial(self.set_cache, args[0], info, **kwargs))
                return info
            except Exception as e:

                # (youtube_dl.utils.ExtractorError, youtube_dl.utils.DownloadError)
                # I hope I don't have to deal with ContentTooShortError's
                if asyncio.iscoroutinefunction(on_error):
                    asyncio.ensure_future(on_error(e), loop=loop)

                elif asyncio.iscoroutine(on_error):
                    asyncio.ensure_future(on_error, loop=loop)

                else:
                    loop.call_soon_threadsafe(on_error, e)

                if retry_on_error:
                    return await self.safe_extract_info(loop, *args, **kwargs)
        else:
            info = await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))
            loop.run_in_executor(self.thread_pool, functools.partial(self.set_cache, args[0], info, **kwargs))
            return info

    async def safe_extract_info(self, loop, *args, **kwargs):
        info = await loop.run_in_executor(self.thread_pool, functools.partial(self.get_cache, args[0], **kwargs))
        if info:
            return info

        info = await loop.run_in_executor(self.thread_pool, functools.partial(self.safe_ytdl.extract_info, *args, **kwargs))
        loop.run_in_executor(self.thread_pool, functools.partial(self.set_cache, args[0], info, **kwargs))
        return info
