import logging
import shlex
import time
import traceback
from datetime import timedelta
from io import BytesIO
from textwrap import dedent

import asyncio
from musicbot.commands import command
from musicbot.constants import DISCORD_MSG_CHAR_LIMIT
from musicbot.exceptions import (CommandError, PermissionsError,
                                 WrongEntryTypeError)
from musicbot.structures import Response
from musicbot.utils import sane_round_int

log = logging.getLogger(__name__)


@command("play")
async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
    """
    Usage:
        {command_prefix}play song_link
        {command_prefix}play text to search for

    Adds the song to the playlist.  If a link is not provided, the first
    result from a youtube search is added to the queue.
    """

    song_url = song_url.strip('<>')

    if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
        raise PermissionsError(
            "You have reached your playlist item limit (%s)" % permissions.max_songs, expire_in=30
        )

    if leftover_args:
        song_url = ' '.join([song_url, *leftover_args])

    try:
        info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
    except Exception as e:
        raise CommandError(e, expire_in=30)

    if not info:
        raise CommandError("That video cannot be played.", expire_in=30)

    # abstract the search handling away from the user
    # our ytdl options allow us to use search strings as input urls
    if info.get('url', '').startswith('ytsearch'):
        # log.info("[Command:play] Searching for \"%s\"" % song_url)
        info = await self.downloader.extract_info(
            player.playlist.loop,
            song_url,
            download=False,
            process=True,    # ASYNC LAMBDAS WHEN
            on_error=lambda e: asyncio.ensure_future(
                self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
            retry_on_error=True
        )

        if not info:
            raise CommandError(
                "Error extracting info from search string, youtubedl returned no data.  "
                "You may need to restart the bot if this continues to happen.", expire_in=30
            )

        if not all(info.get('entries', [])):
            # empty list, no data
            return

        song_url = info['entries'][0]['webpage_url']
        info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
        # But this is probably fine

    # TODO: Possibly add another check here to see about things like the bandcamp issue
    # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

    if 'entries' in info:
        # I have to do exe extra checks anyways because you can request an arbitrary number of search results
        if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
            raise PermissionsError("You are not allowed to request playlists", expire_in=30)

        # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
        num_songs = sum(1 for _ in info['entries'])

        if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
            raise PermissionsError(
                "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                expire_in=30
            )

        # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
        if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
            raise PermissionsError(
                "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                    num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                expire_in=30
            )

        if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
            try:
                return await play_playlist_async(self, player, channel, author, permissions, song_url, info['extractor'])
            except CommandError:
                raise
            except Exception as e:
                traceback.print_exc()
                raise CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

        t0 = time.time()

        # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
        # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
        # I don't think we can hook into it anyways, so this will have to do.
        # It would probably be a thread to check a few playlists and get the speed from that
        # Different playlists might download at different speeds though
        wait_per_song = 1.2

        procmesg = await self.safe_send_message(
            channel,
            'Gathering playlist information for {} songs{}'.format(
                num_songs,
                ', ETA: {} seconds'.format(self._fixg(
                    num_songs * wait_per_song)) if num_songs >= 10 else '.'))


        # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
        #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

        entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

        tnow = time.time()
        ttime = tnow - t0
        listlen = len(entry_list)
        drop_count = 0

        if permissions.max_song_length:
            for e in entry_list.copy():
                if e.duration > permissions.max_song_length:
                    player.playlist.entries.remove(e)
                    entry_list.remove(e)
                    drop_count += 1
                    # Im pretty sure there's no situation where this would ever break
                    # Unless the first entry starts being played, which would make this a race condition
            if drop_count:
                log.info("Dropped %s songs" % drop_count)

        log.info("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            listlen,
            self._fixg(ttime),
            ttime / listlen,
            ttime / listlen - wait_per_song,
            self._fixg(wait_per_song * num_songs))
        )

        await self.safe_delete_message(procmesg)

        if not listlen - drop_count:
            raise CommandError(
                "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                expire_in=30
            )

        reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
        btext = str(listlen - drop_count)

    else:
        if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
            raise PermissionsError(
                "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                expire_in=30
            )

        try:
            entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

        except WrongEntryTypeError as e:
            if e.use_url == song_url:
                log.info("[Warning] Determined incorrect entry type, but suggested url is the same.  Help.")

            return await cmd_play(self, player, channel, author, permissions, leftover_args, e.use_url)

        reply_text = "Enqueued **%s** to be played. Position in queue: %s"
        btext = entry.title

    if position == 1 and player.is_stopped:
        position = 'Up next!'
        reply_text %= (btext, position)

    else:
        try:
            time_until = await player.playlist.estimate_time_until(position, player)
            reply_text += ' - estimated time until playing: %s'
        except:
            traceback.print_exc()
            time_until = ''

        reply_text %= (btext, position, time_until)

    return Response(reply_text, delete_after=30)


async def play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
    """
    Secret handler to use the async wizardry to make playlist queuing non-"blocking"
    """

    info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

    if not info:
        raise CommandError("That playlist cannot be played.")

    num_songs = sum(1 for _ in info['entries'])
    t0 = time.time()

    busymsg = await self.safe_send_message(
        channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title

    if extractor_type == 'youtube:playlist':
        try:
            entries_added = await player.playlist.async_process_youtube_playlist(
                playlist_url, channel=channel, author=author)
            # TODO: Add hook to be called after each song
            # TODO: Add permissions

        except Exception:
            traceback.print_exc()
            raise CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

    elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
        try:
            entries_added = await player.playlist.async_process_sc_bc_playlist(
                playlist_url, channel=channel, author=author)
            # TODO: Add hook to be called after each song
            # TODO: Add permissions

        except Exception:
            traceback.print_exc()
            raise CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

    songs_processed = len(entries_added)
    drop_count = 0
    skipped = False

    if permissions.max_song_length:
        for e in entries_added.copy():
            if e.duration > permissions.max_song_length:
                try:
                    player.playlist.entries.remove(e)
                    entries_added.remove(e)
                    drop_count += 1
                except:
                    pass

        if drop_count:
            log.info("Dropped %s songs" % drop_count)

        if player.current_entry and player.current_entry.duration > permissions.max_song_length:
            await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
            self.server_specific_data[channel.server]['last_np_msg'] = None
            skipped = True
            player.skip()
            entries_added.pop()

    await self.safe_delete_message(busymsg)

    songs_added = len(entries_added)
    tnow = time.time()
    ttime = tnow - t0
    wait_per_song = 1.2
    # TODO: actually calculate wait per song in the process function and return that too

    # This is technically inaccurate since bad songs are ignored but still take up time
    log.info("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
        songs_processed,
        num_songs,
        self._fixg(ttime),
        ttime / num_songs,
        ttime / num_songs - wait_per_song,
        self._fixg(wait_per_song * num_songs))
    )

    if not songs_added:
        basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
        if skipped:
            basetext += "\nAdditionally, the current song was skipped for being too long."

        raise CommandError(basetext, expire_in=30)

    return Response("Enqueued {} songs to be played in {} seconds".format(
        songs_added, self._fixg(ttime, 1)), delete_after=30)


@command("search")
async def cmd_search(self, player, channel, author, permissions, leftover_args):
    """
    Usage:
        {command_prefix}search [service] [number] query

    Searches a service for a video and adds it to the queue.
    - service: any one of the following services:
        - youtube (yt) (default if unspecified)
        - soundcloud (sc)
        - yahoo (yh)
    - number: return a number of video results and waits for user to choose one
      - defaults to 1 if unspecified
      - note: If your search query starts with a number,
              you must put your query in quotes
        - ex: {command_prefix}search 2 "I ran seagulls"
    """

    if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
        raise PermissionsError(
            "You have reached your playlist item limit (%s)" % permissions.max_songs,
            expire_in=30
        )

    def argcheck():
        if not leftover_args:
            raise CommandError(
                "Please specify a search query.\n%s" % dedent(
                    cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                expire_in=60
            )

    argcheck()

    try:
        leftover_args = shlex.split(' '.join(leftover_args))
    except ValueError:
        raise CommandError("Please quote your search query properly.", expire_in=30)

    service = 'youtube'
    items_requested = 3
    max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
    services = {
        'youtube': 'ytsearch',
        'soundcloud': 'scsearch',
        'yahoo': 'yvsearch',
        'yt': 'ytsearch',
        'sc': 'scsearch',
        'yh': 'yvsearch'
    }

    if leftover_args[0] in services:
        service = leftover_args.pop(0)
        argcheck()

    if leftover_args[0].isdigit():
        items_requested = int(leftover_args.pop(0))
        argcheck()

        if items_requested > max_items:
            raise CommandError("You cannot search for more than %s videos" % max_items)

    # Look jake, if you see this and go "what the fuck are you doing"
    # and have a better idea on how to do this, i'd be delighted to know.
    # I don't want to just do ' '.join(leftover_args).strip("\"'")
    # Because that eats both quotes if they're there
    # where I only want to eat the outermost ones
    if leftover_args[0][0] in '\'"':
        lchar = leftover_args[0][0]
        leftover_args[0] = leftover_args[0].lstrip(lchar)
        leftover_args[-1] = leftover_args[-1].rstrip(lchar)

    search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

    search_msg = await self.send_message(channel, "Searching for videos...")

    try:
        info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

    except Exception as e:
        await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
        return
    else:
        await self.safe_delete_message(search_msg)

    if not info:
        return Response("No videos found.", delete_after=30)

    def check(m):
        return (
            m.content.lower()[0] in 'yn' or
            # hardcoded function name weeee
            m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
            m.content.lower().startswith('exit'))

    for e in info['entries']:
        result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
            info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

        confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n` or `exit`")
        response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

        if not response_message:
            await self.safe_delete_message(result_message)
            await self.safe_delete_message(confirm_message)
            return Response("Ok nevermind.", delete_after=30)

        # They started a new search query so lets clean up and bugger off
        elif response_message.content.startswith(self.config.command_prefix) or \
                response_message.content.lower().startswith('exit'):

            await self.safe_delete_message(result_message)
            await self.safe_delete_message(confirm_message)
            return

        if response_message.content.lower().startswith('y'):
            await self.safe_delete_message(result_message)
            await self.safe_delete_message(confirm_message)
            await self.safe_delete_message(response_message)

            await cmd_play(self, player, channel, author, permissions, [], e['webpage_url'])

            return Response("Alright, coming right up!", delete_after=30)
        else:
            await self.safe_delete_message(result_message)
            await self.safe_delete_message(confirm_message)
            await self.safe_delete_message(response_message)

    return Response("Oh well :frowning:", delete_after=30)


@command("np")
async def cmd_np(self, player, channel, server, message):
    """
    Usage:
        {command_prefix}np

    Displays the current song in chat.
    """

    if player.current_entry:
        if self.server_specific_data[server]['last_np_msg']:
            await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
            self.server_specific_data[server]['last_np_msg'] = None

        song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
        song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
        prog_str = '`[%s/%s]`' % (song_progress, song_total)

        if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
            np_text = "Now Playing: **%s** added by **%s** %s\n" % (
                player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
        else:
            np_text = "Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str)

        self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
        await self._manual_delete_check(message)
    else:
        return Response(
            'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix),
            delete_after=30
        )


@command("pause")
async def cmd_pause(self, player):
    """
    Usage:
        {command_prefix}pause

    Pauses playback of the current song.
    """

    if player.is_playing:
        player.pause()

    else:
        raise CommandError('Player is not playing.', expire_in=30)


@command("resume")
async def cmd_resume(self, player):
    """
    Usage:
        {command_prefix}resume

    Resumes playback of a paused song.
    """

    if player.is_paused:
        player.resume()

    else:
        raise CommandError('Player is not paused.', expire_in=30)


@command("shuffle")
async def cmd_shuffle(self, channel, player, leftover_args, seed=None):
    """
    Usage:
        {command_prefix}shuffle [seed]

    Shuffles the playlist.
    """

    if leftover_args:
        seed = ' '.join([seed, *leftover_args])

    player.playlist.shuffle(seed)

    return Response("Shuffled playlist!", delete_after=15)


@command("clear")
async def cmd_clear(self, player, author):
    """
    Usage:
        {command_prefix}clear

    Clears the playlist.
    """

    player.playlist.clear()
    return Response(':put_litter_in_its_place:', delete_after=20)


@command("skip")
async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
    """
    Usage:
        {command_prefix}skip

    Skips the current song when enough votes are cast, or by the bot owner.
    """

    if player.is_stopped:
        raise CommandError("Can't skip! The player is not playing!", expire_in=20)

    if not player.current_entry:
        if player.playlist.peek():
            if player.playlist.peek()._is_downloading:
                log.info(player.playlist.peek()._waiting_futures[0].__dict__)
                return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

            elif player.playlist.peek().is_downloaded:
                log.info("The next song will be played shortly.  Please wait.")
            else:
                log.info("Something odd is happening.  "
                      "You might want to restart the bot if it doesn't start working.")
        else:
            log.info("Something strange is happening.  "
                  "You might want to restart the bot if it doesn't start working.")

    if author.id == self.config.owner_id or permissions.instaskip:
        player.skip()
        await self._manual_delete_check(message)
        return

    # TODO: ignore person if they're deaf or take them out of the list or something?
    # Currently is recounted if they vote, deafen, then vote

    num_voice = sum(1 for m in voice_channel.voice_members if not (
        m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

    num_skips = player.skip_state.add_skipper(author.id, message)

    skips_remaining = min(
        self.config.skips_required,
        sane_round_int(num_voice * self.config.skip_ratio_required)
    ) - num_skips

    if skips_remaining <= 0:
        player.skip()
        return Response(
            'your skip for **{title}** was acknowledged.'
            '\nThe vote to skip has been passed.{extra}'.format(
                title=player.current_entry.title,
                extra=' Next song coming up!' if player.playlist.peek() else ''
            ),
            reply=True,
            delete_after=20
        )

    else:
        # TODO: When a song gets skipped, delete the old x needed to skip messages
        return Response(
            'your skip for **{title}** was acknowledged.'
            '\n**{remaining}** more {votes} required to vote to skip this song.'.format(
                title=player.current_entry.title,
                remaining=skips_remaining,
                votes='person is' if skips_remaining == 1 else 'people are'
            ),
            reply=True,
            delete_after=20
        )


@command("volume")
async def cmd_volume(self, message, player, new_volume=None):
    """
    Usage:
        {command_prefix}volume (+/-)[volume]

    Sets the playback volume. Accepted values are from 1 to 100.
    Putting + or - before the volume will make the volume change relative to the current volume.
    """

    if not new_volume:
        return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

    relative = False
    if new_volume[0] in '+-':
        relative = True

    try:
        new_volume = int(new_volume)

    except ValueError:
        raise CommandError('{} is not a valid number'.format(new_volume), expire_in=20)

    if relative:
        vol_change = new_volume
        new_volume += (player.volume * 100)

    old_volume = int(player.volume * 100)

    if 0 < new_volume <= 100:
        player.volume = new_volume / 100.0

        return Response('updated volume from %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

    else:
        if relative:
            raise CommandError(
                'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                    old_volume,
                    vol_change,
                    old_volume + vol_change, 1 - old_volume, 100 - old_volume
                ), expire_in=20)
        else:
            raise CommandError(
                'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume),
                expire_in=20
            )


@command("queue")
async def cmd_queue(self, channel, player, sendas=None):
    """
    Usage:
        {command_prefix}queue

    Prints the current song queue.
    """

    if sendas:
        sendall = (sendas.lower() in ['file', 'full', 'all'])
    else:
        sendall = False

    lines = []
    unlisted = 0
    andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

    if player.current_entry:
        song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
        song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
        prog_str = '`[%s/%s]`' % (song_progress, song_total)

        if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
            lines.append("Now Playing: **%s** added by **%s** %s\n" % (
                player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
        else:
            lines.append("Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str))

    for i, item in enumerate(player.playlist, 1):
        if item.meta.get('channel', False) and item.meta.get('author', False):
            nextline = '`{}.` **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
        else:
            nextline = '`{}.` **{}**'.format(i, item.title).strip()

        currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

        if (currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT) and not sendall:
            if currentlinesum + len(andmoretext):
                unlisted += 1
                continue

        lines.append(nextline)

    if unlisted:
        lines.append('\n*... and %s more*' % unlisted)

    if not lines:
        lines.append(
            'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

    message = '\n'.join(lines)

    if sendall:
        with BytesIO() as data:
            data.writelines(x.encode('utf8') + b'\n' for x in lines)
            data.seek(0)
            return await self.send_file(
                channel,
                data,
                filename='musicbot-full-queue.txt'
            )

    return Response(message, delete_after=30)


@command("seek")
async def cmd_seek(self, message, player, seek=None):
    """
    Usage:
        {command_prefix}seek [seconds]

    Seeks the player to a specific time in seconds.
    """

    if player.is_stopped:
        raise CommandError("Can't seek! The player is not playing!", expire_in=20)

    if not seek:
        return Response('A time is required to seek.', reply=True, delete_after=20)

    try:
        seek = int(seek.strip())
        if seek < 0:
            raise ValueError()
    except ValueError:
        return Response('The time you have given is an invalid number.', reply=True, delete_after=20)

    player.seek(seek)

    return Response('Seeked to %d seconds!' % (seek), delete_after=20)
