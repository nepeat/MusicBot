all_commands = {}


def command(name):
    def decorate(f):
        if name in all_commands:
            raise Exception("dev is dumb. (duped command)")

        all_commands[name] = f
        return f
    return decorate

from musicbot.commands import *
