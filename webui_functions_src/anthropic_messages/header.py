"""
name: Anthropic Messages Manifold
description: Anthropic Messages API を用いた Manifold Pipe.
"""

# Anthropic docs: streaming is required when max_tokens is greater than 21,333.
_ANTHROPIC_STREAMING_REQUIRED_MAX_TOKENS = 21333
