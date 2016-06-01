import decimal
from musicbot.config import Config, ConfigDefaults
from musicbot.permissions import Permissions, PermissionsDefaults


def sane_round_int(x):
    return int(decimal.Decimal(x).quantize(1, rounding=decimal.ROUND_HALF_UP))


def load_config(context):
    context.config = Config(ConfigDefaults.options_file)
    context.permissions = Permissions(PermissionsDefaults.perms_file, grant_all=[context.config.owner_id])
