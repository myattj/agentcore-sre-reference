from strands.models.bedrock import BedrockModel


# Default model. Tenants can override via tenant.model_id in config.
# global.anthropic.claude-sonnet-4-6 = global cross-region inference profile
# for the latest Claude Sonnet 4.6 (released early 2026).
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def load_model(model_id: str | None = None) -> BedrockModel:
    """Get a Bedrock model client. Pass model_id from tenant config to
    avoid hardcoding the model anywhere except this default."""
    return BedrockModel(model_id=model_id or DEFAULT_MODEL_ID)
