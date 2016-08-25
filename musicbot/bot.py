import inspect
import logging
import os
import sys
import traceback
from collections import defaultdict

import aiohttp
import asyncio
import discord
import raven
import redis
from discord.http import _func_
from discord.enums import ChannelType
from discord.voice_client import VoiceClient
from musicbot import downloader, exceptions
from musicbot.commands import all_commands
from musicbot.connections import redis_pool
from musicbot.player import MusicPlayer
from musicbot.playlist import Playlist
from musicbot.structures import Response, SkipState
from musicbot.utils import fixg, load_config, migrate_redis

# Logging
logging.basicConfig(level=logging.INFO)

if "DEBUG" in os.environ:
    logging.basicConfig(level=logging.DEBUG)

log = logging.getLogger(__name__)


class MusicBot(discord.Client):
    def __init__(self):
        self.sentry = raven.Client(dsn=os.environ.get("SENTRY_DSN", None))
        self.redis = redis.StrictRedis(connection_pool=redis_pool)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        self.players = {}
        self.aiolocks = defaultdict(asyncio.Lock)
        self.exit_signal = None
        self.init_ok = False

        load_config(self)
        migrate_redis(self.redis)

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/MODIFIED'

    def __del__(self):
        try:
            if not self.http.session.closed:
                self.http.session.close()
        except:
            pass

        try:
            if not self.aiosession.closed:
                self.aiosession.close()
        except:
            pass

    def _get_owner(self, *, server=None, voice=False):
        return discord.utils.find(
            lambda m: m.id == self.config.owner_id and m.voice_channel if voice else True,
            server.members if server else self.get_all_members()
        )

    async def _join_startup_channels(self, channels):
        joined_servers = []
        channel_map = {c.server: c for c in channels}

        for server in self.servers:
            if server.unavailable or server in channel_map:
                continue

            if server.me.voice_channel:
                log.debug("Found resumable voice channel {0.server.name}/{0.name}".format(server.me.voice_channel))

                channel_map[server] = server.me.voice_channel

        for (server, channel) in channel_map.items():
            if server in joined_servers:
                log.info("Already joined a channel in {}, skipping", server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                log.info("Attempting to join {0.server.name}/{0.name}".format(channel))

                chperms = channel.permissions_for(channel.server.me)

                if not chperms.connect:
                    log.info("Cannot join channel \"{}\", no permission.", channel.name)
                    continue

                elif not chperms.speak:
                    log.info("Will not join channel \"{}\", no permission to speak.", channel.name)
                    continue

                try:
                    player = await self.get_player(channel, create=True)
                    log.info("Joined {0.server.name}/{0.name}".format(channel))

                    if player.is_stopped:
                        player.play()
                except Exception as e:
                    log.error("Failed to join %s", channel.name)

            elif channel:
                log.info("Not joining {0.server.name}/{0.name}, that's a text channel.".format(channel))

            else:
                log.info("Invalid channel thing: %s", channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def get_voice_client(self, channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        async with self.aiolocks[_func_()]:
            if self.is_voice_connected(channel.server):
                return self.voice_client_in(channel.server)

            vc = await self.join_voice_channel(channel)
            vc.ws._keep_alive.name = 'VoiceClient Keepalive'

            return vc

    async def reconnect_voice_client(self, server, *, sleep=0.1, create_with_channel=None):
        async with self.aiolocks[_func_() + ':' + server.id]:
            vc = self.voice_client_in(server)

            if not (vc or create_with_channel):
                return

            _paused = False

            player = None
            if server.id in self.players:
                player = self.players[server.id]
                if player.is_playing:
                    player.pause()
                    _paused = True

            if not create_with_channel:
                try:
                    await vc.disconnect()
                except:
                    log.info("Error disconnecting during reconnect")
                    traceback.print_exc()

                if sleep:
                    await asyncio.sleep(sleep)

            if player:
                if not create_with_channel:
                    new_vc = await self.get_voice_client(vc.channel)
                else:
                    # noinspection PyTypeChecker
                    new_vc = await self.get_voice_client(create_with_channel)

                await player.reload_voice(new_vc)

                if player.is_paused and _paused:
                    player.resume()

    async def disconnect_voice_client(self, server):
        vc = self.voice_client_in(server)
        if not vc:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.server)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.server.id, vchannel.id, mute, deaf)

    def get_player_in(self, server: discord.Server) -> MusicPlayer:
        return self.players.get(server.id, None)

    async def get_player(self, channel, create=False) -> MusicPlayer:
        server = channel.server

        async with self.aiolocks[_func_()]:
            if server.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        'The bot is not in a voice channel.  '
                        'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self, channel.server.id)
                player = MusicPlayer(self, voice_client, playlist) \
                    .on('play', self.on_player_play) \
                    .on('error', self.on_player_error)

                player.skip_state = SkipState()
                self.players[server.id] = player

            async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
                if self.players[server.id].voice_client not in self.voice_clients:
                    log.info("oh no reconnect needed")
                    await self.reconnect_voice_client(server, create_with_channel=channel)

            return self.players[server.id]

    async def on_player_play(self, player, entry):
        player.skip_state.reset()

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = '%s - your song **%s** is now playing in %s!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in %s: **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

    async def on_player_error(self, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            traceback.print_exception(ex.__class__, ex, ex.__traceback__)

    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                log.warning("Cannot send message to %s, no permission" % dest.name)

        except discord.NotFound:
            if not quiet:
                log.warning("Cannot send message to %s, invalid channel?" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                log.warning("Cannot delete message \"%s\", no permission" % message.clean_content)

        except discord.NotFound:
            if not quiet:
                log.warning("Cannot delete message \"%s\", message not found" % message.clean_content)

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                log.warning("Cannot edit message \"%s\", message not found" % message.clean_content)
            if send_if_fail:
                if not quiet:
                    log.info("Sending instead")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            pass

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: # Can be ignored
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: # Can be ignored
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))
        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Token in the options file.  "
                "Remember that each field should be on their own line.")
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self._cleanup()
            except Exception as e:
                log.info("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.info("Exception in", event)
            log.info(ex.message)

            await asyncio.sleep(2)  # don't ask
            await self.logout()
        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()
        else:
            self.sentry.captureException()
            traceback.print_exc()

    async def on_resumed(self):
        log.info("Reconnected to Discord.")
        for vc in self.the_voice_clients.values():
            vc.main_ws = self.ws

    async def on_ready(self):
        self.ws._keep_alive.name = 'Gateway Keepalive'
        log.info('Connected!')
        self.init_ok = True

        if self.config.owner_id == self.user.id:
            raise exceptions.HelpfulError(
                "Your OwnerID is incorrect or you've used the wrong credentials.",

                "The bot needs its own account to function.  "
                "The OwnerID is the id of the owner, not the bot.  "
                "Figure out which one is which and use the correct information.")

        log.info("ID:%s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner(voice=True) or self._get_owner()

        if self.servers:
            if owner:
                log.info("Owner:%s/%s#%s" % (owner.id, owner.name, owner.discriminator))
            else:
                log.info("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            log.info('Server List:')
            [log.info(' - ' + s.name) for s in self.servers]
        else:
            log.info("Owner unavailable, bot is not on any servers.")

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            log.info("Bound to text channels:")
            [log.info(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
        else:
            log.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            log.info("Autojoining voice chanels:")
            [log.info(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            autojoin_channels = chlist

        else:
            log.info("Not autojoining any voice channels")
            autojoin_channels = set()

        log.info("Options:")

        log.info("  Command prefix: " + self.config.command_prefix)
        log.info("  Default volume: %s%%" % int(self.config.default_volume * 100))
        log.info("  Skip threshold: %s votes or %s%%" % (
            self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
        log.info("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        log.info("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            log.info("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(autojoin_channels)

        # t-t-th-th-that's all folks!

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.info("Ignoring command from myself (%s)" % message.content)
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return

        command, *args = message_content.split()  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = all_commands.get(command, None)["f"]
        if not handler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id):
                await self.send_message(message.channel, 'You cannot use this bot in private messages.')
                return

        else:
            log.info("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}

            # Pop the self param to prevent docstrings on all commands..
            if params.pop("self", None):
                handler_kwargs['self'] = self

            if params.pop("bot", None):
                handler_kwargs['bot'] = self

            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = self.get_player_in(message.server)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            if params.pop('redis', None):
                handler_kwargs['redis'] = self.redis

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.info("{0.__class__}: {0.message}".format(e))

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n%s\n```' % e.message,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            traceback.print_exc()
            self.sentry.captureException()
            await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def on_voice_state_update(self, before, after):
        pass

    async def on_server_update(self, before: discord.Server, after: discord.Server):
        if before.region != after.region:
            log.info("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)

    async def on_server_join(self, server: discord.Server):
        log.info("Bot has been joined server: {}".format(server.name))

    async def on_server_remove(self, server: discord.Server):
        log.info("Bot has been removed from server: {}".format(server.name))

        log.debug('Updated server list:')
        [log.debug(' - ' + s.name) for s in self.servers]

        if server.id in self.players:
            self.players.pop(server.id).kill()

    async def on_server_available(self, server: discord.Server):
        if not self.init_ok:
            return

        log.info("Server \"{}\" has become available.".format(server.name))
        player = self.get_player_in(server)

        if player and player.is_paused:
            av_paused = self.server_specific_data[server]['availability_paused']

            if av_paused:
                log.info("Resuming player in \"{}\" due to availability.".format(server.name))
                self.server_specific_data[server]['availability_paused'] = False
                player.resume()

    async def on_server_unavailable(self, server: discord.Server):
        log.info("Server \"{}\" has become unavailable.".format(server.name))
        player = self.get_player_in(server)

        if player and player.is_playing:
            log.info("Pausing player in \"{}\" due to unavailability.".format(server.name))
            self.server_specific_data[server]['availability_paused'] = True
            player.pause()

if __name__ == '__main__':
    bot = MusicBot()
    bot.run()
