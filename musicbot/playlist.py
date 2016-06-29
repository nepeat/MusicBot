import datetime
import json
import logging
import random
import traceback
from collections import deque
from itertools import islice

import redis

import asyncio
from musicbot.commands.music import cmd_play
from musicbot.connections import redis_pool
from musicbot.entry import URLPlaylistEntry
from musicbot.exceptions import ExtractionError, RetryPlay, WrongEntryTypeError
from musicbot.lib.event_emitter import EventEmitter
from musicbot.utils import get_header

log = logging.getLogger(__name__)


class Playlist(EventEmitter):
    """
        A playlist is manages the list of songs that will be played.
    """

    def __init__(self, bot, serverid):
        super().__init__()
        self.bot = bot
        self.serverid = serverid
        self.redis = redis.StrictRedis(connection_pool=redis_pool)
        self.loop = bot.loop
        self.downloader = bot.downloader
        self.entries = deque()

        self.load_saved()

    def __iter__(self):
        return iter(self.entries)

    def load_saved(self):
        items = self.redis.lrange("musicbot:queue:" + self.serverid, 0, -1)

        for item in items:
            meta = {}

            try:
                data = json.loads(item)
            except json.JSONDecodeError as e:
                log.error(e)
                log.error(item)
                continue

            # Fix for the to_dict() returning json derp.
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError as e:
                    log.error(e)
                    log.error(data)
                    continue

            if "channel" in data["meta"] and "author" in data["meta"]:
                meta["channel"] = self.bot.get_channel(data["meta"]["channel"])
                meta["author"] = meta["channel"].server.get_member(data["meta"]["author"])

            meta["seek"] = data["meta"].get("seek", 0)

            if "http" in data["url"].lower():
                asyncio.ensure_future(self.add_entry(data["url"], saved=True, **meta), loop=self.bot.loop)
            else:
                self.redis.lrem("musicbot:queue:" + self.serverid, 1, item)

    def shuffle(self, seed=None):
        if seed:
            random.seed(seed)

        random.shuffle(self.entries)
        self.redis.delete("musicbot:queue:" + self.serverid)
        self.redis.rpush("musicbot:queue:" + self.serverid, *[entry.to_json() for entry in self.entries])
        random.seed()

    def clear(self, kill=False, last_entry=None):
        self.entries.clear()

        if kill and last_entry:
            self.redis.lpush("musicbot:queue:" + self.serverid, last_entry.to_json())
        else:
            self.redis.delete("musicbot:queue:" + self.serverid)

    async def add_entry(self, song_url, saved=False, prepend=False, **meta):
        """
            Validates and adds a song_url to be played. This does not start the download of the song.

            Returns the entry & the position it is in the queue.

            :param song_url: The song url to add to the playlist.
            :param meta: Any additional metadata to add to the playlist entry.
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(song_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % song_url)

        # TODO: Sort out what happens next when this happens
        if info.get('_type', None) == 'playlist':
            raise WrongEntryTypeError("This is a playlist.", True, info.get('webpage_url', None) or info.get('url', None))

        if info['extractor'] in ['generic', 'Dropbox']:
            try:
                # unfortunately this is literally broken
                # https://github.com/KeepSafe/aiohttp/issues/758
                # https://github.com/KeepSafe/aiohttp/issues/852
                content_type = await get_header(self.bot.aiosession, info['url'], 'CONTENT-TYPE')
                log.debug("Got content type %s", content_type)

            except asyncio.TimeoutError as e:
                raise ExtractionError("This URL took too long to load.")
            except Exception as e:
                lower_e = str(e).lower()

                if "does not resolve" in lower_e or "no route to host" in lower_e or "invalid argument" in lower_e:
                    raise RetryPlay()

                log.warning("Failed to get content type for url %s (%s)", song_url, e)
                content_type = None

            if content_type:
                if content_type.startswith(('application/', 'image/')):
                    if '/ogg' not in content_type:  # How does a server say `application/ogg` what the actual fuck
                        raise ExtractionError("Invalid content type \"%s\" for url %s" % (content_type, song_url))

                elif not content_type.startswith(('audio/', 'video/')):
                    log.warning("Questionable content type \"%s\" for url %s", content_type, song_url)

        entry = URLPlaylistEntry(
            playlist=self,
            url=song_url,
            title=info.get('title', 'Untitled'),
            duration=info.get('duration', 0) or 0,
            expected_filename=self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry, saved, prepend)
        return entry, len(self.entries)

    async def import_from(self, playlist_url, **meta):
        """
            Imports the songs from `playlist_url` and queues them to be played.

            Returns a list of `entries` that have been enqueued.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """
        position = len(self.entries) + 1
        entry_list = []

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        # Once again, the generic extractor fucks things up.
        if info.get('extractor', None) == 'generic':
            url_field = 'url'
        else:
            url_field = 'webpage_url'

        baditems = 0
        for items in info['entries']:
            if items:
                try:
                    entry = URLPlaylistEntry(
                        playlist=self,
                        url=items[url_field],
                        title=items.get('title', 'Untitled'),
                        duration=items.get('duration', 0) or 0,
                        expected_filename=self.downloader.ytdl.prepare_filename(items),
                        **meta
                    )

                    self._add_entry(entry)
                    entry_list.append(entry)
                except:
                    baditems += 1
                    # Once I know more about what's happening here I can add a proper message
                    traceback.print_exc()
                    log.error(items)
                    log.error("Could not add item")
            else:
                baditems += 1

        if baditems:
            log.info("Skipped %s bad entries", baditems)

        return entry_list, position

    async def async_process_youtube_playlist(self, playlist_url, **meta):
        """
            Processes youtube playlists links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0
        for entry_data in info['entries']:
            if entry_data:
                baseurl = info['webpage_url'].split('playlist?list=')[0]
                song_url = baseurl + 'watch?v=%s' % entry_data['id']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)
                except ExtractionError:
                    baditems += 1
                except Exception as e:
                    baditems += 1
                    log.error("There was an error adding the song {}: {}: {}\n".format(
                        entry_data['id'], e.__class__.__name__, e
                    ))
            else:
                baditems += 1

        if baditems:
            log.info("Skipped %s bad entries" % baditems)

        return gooditems

    async def async_process_sc_bc_playlist(self, playlist_url, **meta):
        """
            Processes soundcloud set and bancdamp album links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0
        for entry_data in info['entries']:
            if entry_data:
                song_url = entry_data['url']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)
                except ExtractionError:
                    baditems += 1
                except Exception as e:
                    baditems += 1
                    log.error("There was an error adding the song {}: {}: {}\n".format(
                        entry_data['id'], e.__class__.__name__, e
                    ))
            else:
                baditems += 1

        if baditems:
            log.info("Skipped %s bad entries" % baditems)

        return gooditems

    def _add_entry(self, entry, saved=False, prepend=False):
        if prepend:
            self.entries.appendleft(entry)
        else:
            self.entries.append(entry)

        if not saved:
            self.redis.hincrby("musicbot:played", entry.url, 1)
            if prepend:
                self.redis.lpush("musicbot:queue:" + self.serverid, entry.to_json())
            else:
                self.redis.rpush("musicbot:queue:" + self.serverid, entry.to_json())
        self.emit('entry-added', playlist=self, entry=entry)

        if self.peek() is entry:
            entry.get_ready_future()

    async def get_next_entry(self, predownload_next=True):
        """
            A coroutine which will return the next song or None if no songs left to play.

            Additionally, if predownload_next is set to True, it will attempt to download the next
            song to be played - so that it's ready by the time we get to it.
        """
        if not self.entries:
            return None

        entry = self.entries.popleft()
        self.redis.lpop("musicbot:queue:" + self.serverid)

        if predownload_next:
            next_entry = self.peek()
            if next_entry:
                next_entry.get_ready_future()

        return await entry.get_ready_future()

    def peek(self):
        """
            Returns the next entry that should be scheduled to be played.
        """
        if self.entries:
            return self.entries[0]

    async def estimate_time_until(self, position, player):
        """
            (very) Roughly estimates the time till the queue will 'position'
        """
        estimated_time = sum([e.duration for e in islice(self.entries, position - 1)])

        # When the player plays a song, it eats the first playlist item, so we just have to add the time back
        if not player.is_stopped and player.current_entry:
            estimated_time += player.current_entry.duration - player.progress

        return datetime.timedelta(seconds=estimated_time)

    def count_for_user(self, user):
        return sum(1 for e in self.entries if e.meta.get('author', None) == user)
