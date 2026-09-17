"""Gateway startup failures that require an operator to change configuration."""

STARTUP_CONFIGURATION_EXIT_CODE = 78


class GatewayConfigurationError(ValueError):
    """Gateway configuration failed validation before service readiness."""
