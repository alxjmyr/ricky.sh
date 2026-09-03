"""Platform-neutral transport outcome categories used at retry boundaries."""


class TransportError(RuntimeError):
    """A transport operation failed with a credential-safe operator message."""


class DeliveryNotPerformedError(TransportError):
    """The transport proves an outbound side effect did not occur."""


class AmbiguousDeliveryError(TransportError):
    """An outbound side effect may have occurred and cannot be replayed safely."""
