import datetime
import json
import logging
import os
import pickle
import random
import traceback
from collections import deque
from itertools import islice

import asyncio
from musicbot.exceptions import ExtractionError, WrongEntryTypeError
from musicbot.lib.event_emitter import EventEmitter
from musicbot.utils import get_header, md5sum

log = logging.getLogger(__name__)


class Playlist(EventEmitter):
    """
        A playlist is manages the list of songs that will be played.
    """

    def __init__(self, bot, serverid):
        super().__init__()
        self.bot = bot
        self.serverid = serverid
        self.loop = bot.loop
        self.downloader = bot.downloader
        self.entries = deque()

        self.load_saved()

    def __iter__(self):
        return iter(self.entries)

    def load_saved(self):
        items = self.bot.redis.lrange("musicbot:queue:" + self.serverid, 0, -1)

        for item in items:
            meta = {}

            try:
                data = json.loads(item)
            except json.JSONDecodeError as e:
                try:
                    data = pickle.loads(item)
                    self._add_entry(data, True)
                except pickle.PickleError as e:
                    log.error(e)
                    log.error(item)

                continue

            if "channel" in data["meta"] and "author" in data["meta"]:
                meta["channel"] = self.bot.get_channel(data["meta"]["channel"])
                meta["author"] = meta["channel"].server.get_member(data["meta"]["author"])

            meta["seek"] = data["meta"].get("seek", 0)

            asyncio.ensure_future(self.add_entry(data["url"], saved=True, **meta), loop=self.bot.loop)

    def shuffle(self, seed=None):
        if seed:
            random.seed(seed)

        random.shuffle(self.entries)
        self.bot.redis.delete("musicbot:queue:" + self.serverid)
        self.bot.redis.rpush("musicbot:queue:" + self.serverid, *[pickle.dump(entry) for entry in self.entries])
        random.seed()

    def clear(self, kill=False, last_entry=None):
        self.entries.clear()

        if kill and last_entry:
            self.bot.redis.lpush("musicbot:queue:" + self.serverid, pickle.dump(last_entry))
        else:
            self.bot.redis.delete("musicbot:queue:" + self.serverid)

    async def add_entry(self, song_url, saved=False, **meta):
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

            except Exception as e:
                log.warning("Failed to get content type for url %s (%s)", song_url, e)
                content_type = None

            if content_type:
                if content_type.startswith(('application/', 'image/')):
                    if '/ogg' not in content_type:  # How does a server say `application/ogg` what the actual fuck
                        raise ExtractionError("Invalid content type \"%s\" for url %s" % (content_type, song_url))

                elif not content_type.startswith(('audio/', 'video/')):
                    log.warning("Questionable content type \"%s\" for url %s", content_type, song_url)

        entry = PlaylistEntry(
            self,
            song_url,
            info.get('title', 'Untitled'),
            info.get('duration', 0) or 0,
            self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry, saved)
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
                    entry = PlaylistEntry(
                        self,
                        items[url_field],
                        items.get('title', 'Untitled'),
                        items.get('duration', 0) or 0,
                        self.downloader.ytdl.prepare_filename(items),
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
            self.bot.redis.hincrby("musicbot:played", entry.url, 1)
            self.bot.redis.rpush("musicbot:queue:" + self.serverid, pickle.dump(entry))
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
        self.bot.redis.lpop("musicbot:queue:" + self.serverid)

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


class PlaylistEntry:
    def __init__(self, playlist, url, title, duration=0, expected_filename=None, **meta):
        self.playlist = playlist
        self.url = url
        self.title = title
        self.duration = duration
        self.expected_filename = expected_filename
        self.meta = meta

        self.filename = None
        self._is_downloading = False
        self._waiting_futures = []
        self.download_folder = self.playlist.downloader.download_folder

    def to_dict(self):
        author = self.meta.get("author", None)
        channel = self.meta.get("channel", None)

        if author and channel:
            authorid = author.id
            channelid = channel.id
        else:
            authorid = None
            channelid = None

        return json.dumps({
            "url": self.url,
            "meta": {
                "author": authorid,
                "channel": channelid,
                "seek": self.meta.get("seek", 0)
            }
        })

    @property
    def is_downloaded(self):
        if self._is_downloading:
            return False

        return bool(self.filename)

    # noinspection PyTypeChecker
    async def _download(self):
        if self._is_downloading:
            return

        self._is_downloading = True
        try:
            # Ensure the folder that we're going to move into exists.
            if not os.path.exists(self.download_folder):
                os.makedirs(self.download_folder)

            # self.expected_filename: audio_cache\youtube-9R8aSKwTEMg-NOMA_-_Brain_Power.m4a
            extractor = os.path.basename(self.expected_filename).split('-')[0]

            # the generic extractor requires special handling
            if extractor == 'generic':
                log.debug("Handling generic")
                flistdir = [f.rsplit('-', 1)[0] for f in os.listdir(self.download_folder)]
                expected_fname_noex, fname_ex = os.path.basename(self.expected_filename).rsplit('.', 1)

                if expected_fname_noex in flistdir:
                    try:
                        rsize = int(await get_header(self.playlist.bot.aiosession, self.url, 'CONTENT-LENGTH'))
                    except:
                        rsize = 0

                    lfile = os.path.join(
                        self.download_folder,
                        os.listdir(self.download_folder)[flistdir.index(expected_fname_noex)]
                    )

                    log.debug("Resolved %s to %s" % (self.expected_filename, lfile))
                    lsize = os.path.getsize(lfile)
                    log.debug("Remote size: %s Local size: %s" % (rsize, lsize))

                    if lsize != rsize:
                        await self._really_download(hash=True)
                    else:
                        self.filename = lfile

                else:
                    await self._really_download(hash=True)

            else:
                ldir = os.listdir(self.download_folder)
                flistdir = [f.rsplit('.', 1)[0] for f in ldir]
                expected_fname_base = os.path.basename(self.expected_filename)
                expected_fname_noex = expected_fname_base.rsplit('.', 1)[0]

                # idk wtf this is but its probably legacy code
                # or i have youtube to blame for changing shit again

                if expected_fname_base in ldir:
                    self.filename = os.path.join(self.download_folder, expected_fname_base)
                    log.info("Cached: %s", self.url)

                elif expected_fname_noex in flistdir:
                    log.info("Cached (different extension): %s", self.url)
                    self.filename = os.path.join(self.download_folder, ldir[flistdir.index(expected_fname_noex)])
                    log.info("Expected %s, got %s" % (
                        self.expected_filename.rsplit('.', 1)[-1],
                        self.filename.rsplit('.', 1)[-1]
                    ))

                else:
                    await self._really_download()

            # Trigger ready callbacks.
            self._for_each_future(lambda future: future.set_result(self))

        except Exception as e:
            traceback.print_exc()
            self._for_each_future(lambda future: future.set_exception(e))

        finally:
            self._is_downloading = False

    # noinspection PyShadowingBuiltins
    async def _really_download(self, *, hash=False):
        log.info("Started: %s", self.url)

        try:
            result = await self.playlist.downloader.extract_info(self.playlist.loop, self.url, download=True)
        except Exception as e:
            raise ExtractionError(e)

        log.info("Completed: %s", self.url)

        if result is None:
            raise ExtractionError("ytdl broke and hell if I know why")
            # What the fuck do I do now?

        self.filename = unhashed_fname = self.playlist.downloader.ytdl.prepare_filename(result)

        if hash:
            # insert the 8 last characters of the file hash to the file name to ensure uniqueness
            self.filename = md5sum(unhashed_fname, 8).join('-.').join(unhashed_fname.rsplit('.', 1))

            if os.path.isfile(self.filename):
                # Oh bother it was actually there.
                os.unlink(unhashed_fname)
            else:
                # Move the temporary file to it's final location.
                os.rename(unhashed_fname, self.filename)

    def get_ready_future(self):
        """
        Returns a future that will fire when the song is ready to be played. The future will either fire with the result (being the entry) or an exception
        as to why the song download failed.
        """
        future = asyncio.Future()
        if self.is_downloaded:
            # In the event that we're downloaded, we're already ready for playback.
            future.set_result(self)

        else:
            # If we request a ready future, let's ensure that it'll actually resolve at one point.
            asyncio.ensure_future(self._download())
            self._waiting_futures.append(future)

        return future

    def _for_each_future(self, cb):
        """
            Calls `cb` for each future that is not cancelled. Absorbs and logs any errors that may have occurred.
        """
        futures = self._waiting_futures
        self._waiting_futures = []

        for future in futures:
            if future.cancelled():
                continue

            try:
                cb(future)

            except:
                traceback.print_exc()

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)
