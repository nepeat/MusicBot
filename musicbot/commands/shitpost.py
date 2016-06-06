from musicbot.commands import command
from musicbot.commands.music import cmd_play
from musicbot.structures import Response
from musicbot.utils import weighted_choice


@command("surprise")
async def cmd_surprise(self, player, channel, author, permissions, redis):
    urls = redis.hgetall("musicbot:played")
    url = weighted_choice(urls)

    if url:
        return await cmd_play(self, player, channel, author, permissions, None, url)
    else:
        return Response(
            "There are no songs that can be replayed. Play a few songs and try this command again.",
            delete_after=25
        )
