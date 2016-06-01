import decimal
import shutil
import os
from musicbot.constants import AUDIO_CACHE_PATH


def sane_round_int(x):
    return int(decimal.Decimal(x).quantize(1, rounding=decimal.ROUND_HALF_UP))


def delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
    try:
        shutil.rmtree(path)
        return True
    except:
        try:
            os.rename(path, path + '__')
        except:
            return False
        try:
            shutil.rmtree(path)
        except:
            os.rename(path + '__', path)
            return False

    return True
