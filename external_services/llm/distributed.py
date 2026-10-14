"""Public entry point for the unique registered service/account FIFO."""

class ModelSchedulingError(RuntimeError):
    """Safe failure without Redis URLs, credentials or downstream payloads."""

from .shared_admission import RedisAdmission


def service_pool(service):
    return RedisAdmission(service)
