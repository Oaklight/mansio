"""Serializer implementations for mansio message bus."""

import json


class JSONSerializer:
    """JSON-based serializer. Default for MVP.

    Human-readable, good for debugging and white-box inspection.
    """

    def encode(self, obj: dict) -> str:
        """Encode a dict to a JSON string.

        Args:
            obj: Dictionary to encode.

        Returns:
            JSON-encoded string.
        """
        return json.dumps(obj, ensure_ascii=False)

    def decode(self, data: str) -> dict:
        """Decode a JSON string back to a dict.

        Args:
            data: JSON-encoded string.

        Returns:
            Decoded dictionary.
        """
        return json.loads(data)
