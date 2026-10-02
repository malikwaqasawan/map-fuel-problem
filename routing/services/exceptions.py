class ExternalServiceError(Exception):
    """Base class for errors talking to OpenRouteService."""


class GeocodingError(ExternalServiceError):
    """The place name couldn't be resolved to a US location."""


class RoutingError(ExternalServiceError):
    """ORS couldn't compute a route between the two points."""


class QuotaExceededError(ExternalServiceError):
    """ORS reported rate-limit/quota exhaustion (HTTP 429)."""


class UpstreamTimeoutError(ExternalServiceError):
    """The request to ORS exceeded our configured timeout."""


class UpstreamResponseError(ExternalServiceError):
    """ORS returned a malformed/unexpected response."""
