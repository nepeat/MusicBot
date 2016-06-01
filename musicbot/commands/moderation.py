from functools import wraps

import aiohttp
from discord import Game
from musicbot.commands import command
from musicbot.exceptions import (CommandError, PermissionsError, RestartSignal,
                                 TerminateSignal)
from musicbot.structures import Response
from musicbot.util import load_config


def owner_only(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        # Only allow the owner to use these commands
        orig_msg = self._get_variable('message')

        if not orig_msg or orig_msg.author.id == self.config.owner_id:
            return await func(self, *args, **kwargs)
        else:
            raise PermissionsError("Only the bot admin can use this command.", expire_in=30)

    return wrapper


@command("disconnect")
async def cmd_disconnect(self, server):
    await self.disconnect_voice_client(server)
    return Response(":hear_no_evil:", delete_after=20)


@command("restart")
@owner_only
async def cmd_restart(self, channel):
    await self.safe_send_message(channel, ":wave:")
    await self.disconnect_all_voice_clients()
    raise RestartSignal


@command("shutdown")
@owner_only
async def cmd_shutdown(self, channel):
    await self.safe_send_message(channel, ":wave:")
    await self.disconnect_all_voice_clients()
    raise TerminateSignal


@command("setname")
@owner_only
async def cmd_setname(self, leftover_args, name):
    """
    Usage:
        {command_prefix}setname name

    Changes the bot's username.
    Note: This operation is limited by discord to twice per hour.
    """

    name = ' '.join([name, *leftover_args])

    try:
        await self.edit_profile(username=name)
    except Exception as e:
        raise CommandError(e, expire_in=20)

    return Response(":ok_hand:", delete_after=20)


@command("setnick")
@owner_only
async def cmd_setnick(self, server, channel, leftover_args, nick):
    """
    Usage:
        {command_prefix}setnick nick

    Changes the bot's nickname.
    """

    if not channel.permissions_for(server.me).change_nicknames:
        raise CommandError("Unable to change nickname: no permission.")

    nick = ' '.join([nick, *leftover_args])

    try:
        await self.change_nickname(server.me, nick)
    except Exception as e:
        raise CommandError(e, expire_in=20)

    return Response(":ok_hand:", delete_after=20)


@command("setavatar")
@owner_only
async def cmd_setavatar(self, message, url=None):
    """
    Usage:
        {command_prefix}setavatar [url]

    Changes the bot's avatar.
    Attaching a file and leaving the url parameter blank also works.
    """

    if message.attachments:
        thing = message.attachments[0]['url']
    else:
        thing = url.strip('<>')

    try:
        with aiohttp.Timeout(10):
            async with self.aiosession.get(thing) as res:
                await self.edit_profile(avatar=await res.read())

    except Exception as e:
        raise CommandError("Unable to change avatar: %s" % e, expire_in=20)

    return Response(":ok_hand:", delete_after=20)


@command("setgame")
async def setgame(self, message, leftover_args, game=None):
    """
    Usage:
        {command_prefix}setname [game]

    Changes the bot's game status.
    """
    if game:
        game = Game(name=" ".join([game, *leftover_args]))
    else:
        game = Game(name="")

    await self.change_status(game)

    return Response(":ok_hand:", delete_after=20)


@owner_only
@command("reloadconfig")
def cmd_reloadconfig(bot):
    """
    Usage:
        {command_prefix}reloadconfig

    This reloads the bot configs.
    """
    load_config(bot)
    return Response(":ok_hand:", delete_after=20)
