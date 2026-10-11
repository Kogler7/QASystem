from .clef import ClefFlashClient


class NanoJevServiceClient(ClefFlashClient):
    """Reuse the redacted SystemOne transport with separate service credentials."""

    def __init__(self):
        super().__init__(model="nanojev", env_prefix="NANOJEV", capability="models.nanojev.create")
