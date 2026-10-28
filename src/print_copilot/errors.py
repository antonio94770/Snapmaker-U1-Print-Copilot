"""Exceptions raised by Print Copilot. Every failure carries a readable reason."""


class PrintCopilotError(Exception):
    """Base class for all Print Copilot errors."""


class OrcaNotFoundError(PrintCopilotError):
    """The Snapmaker Orca executable or data directory could not be found."""


class PresetError(PrintCopilotError):
    """A preset is missing, ambiguous or has a broken `inherits` chain."""


class SliceError(PrintCopilotError):
    """The Snapmaker Orca command line failed or produced no usable output."""


class GcodeParseError(PrintCopilotError):
    """A G-code file lacks the summary lines a valid slice must contain."""


class ModelError(PrintCopilotError):
    """A model file cannot be read or contains no usable geometry."""


class ConfError(PrintCopilotError):
    """`Snapmaker_Orca.conf` is missing, unreadable or lacks the expected keys."""


class PrinterError(PrintCopilotError):
    """The printer cannot be reached, refused a request or returned unusable data."""


class ConfirmationError(PrintCopilotError):
    """A gated printer action was not confirmed, or its plan is stale or unknown."""
