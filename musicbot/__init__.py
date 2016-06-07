import discord.opus
from musicbot.bot import MusicBot

if not discord.opus.is_loaded():
    discord.opus.load_opus('opus')

__all__ = ['MusicBot']
