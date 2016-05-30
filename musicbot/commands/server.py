import logging

from musicbot.commands import command
from musicbot.exceptions import CommandError
from musicbot.structures import Response

log = logging.getLogger(__name__)


@command("summon")
async def cmd_summon(self, channel, author, voice_channel):
    """
    Usage:
        {command_prefix}summon

    Call the bot to the summoner's voice channel.
    """

    if not author.voice_channel:
        raise CommandError('You are not in a voice channel!')

    voice_client = self.the_voice_clients.get(channel.server.id, None)
    if voice_client and voice_client.channel.server == author.voice_channel.server:
        await self.move_voice_client(author.voice_channel)
        return

    # move to _verify_vc_perms?
    chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

    if not chperms.connect:
        log.info("Cannot join channel \"%s\", no permission." % author.voice_channel.name)
        return Response(
            "```Cannot join channel \"%s\", no permission.```" % author.voice_channel.name,
            delete_after=25
        )

    elif not chperms.speak:
        log.info("Will not join channel \"%s\", no permission to speak." % author.voice_channel.name)
        return Response(
            "```Will not join channel \"%s\", no permission to speak.```" % author.voice_channel.name,
            delete_after=25
        )

    player = await self.get_player(author.voice_channel, create=True)

    if player.is_stopped:
        player.play()
