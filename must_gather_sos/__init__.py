"""must-gather-sos: an sos-style front end for ``oc adm must-gather``."""

__version__ = "0.1.0"

PROG = "must-gather-sos"


class UserError(Exception):
    """A problem with what the user asked for. Reported without a traceback."""
