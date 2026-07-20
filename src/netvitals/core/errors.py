"""Application errors shared by all clients."""


class AppError(Exception):
    """Business-rule violation or bad input; clients show the message to the user."""
