import discord.opus
from musicbot.bot import MusicBot

OPUS_LIBS = ['opus', 'libopus.so.0']

if not discord.opus.is_loaded():
    for lib in OPUS_LIBS:
        try:
            discord.opus.load_opus(lib)
            break
        except OSError:
            pass

    if not discord.opus.is_loaded():
        raise Exception("Opus library could not be loaded.")

__all__ = ['MusicBot']
