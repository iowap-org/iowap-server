"""
Server-side capability data model.

The relay server does NOT define which capabilities exist. Nodes define their
own capabilities in their YAML config / registration payload. The server only
validates the structure of incoming capability definitions and stores them
alongside the node's heartbeat data.

This model is used for:
  - Validating capability fields in registration and heartbeat payloads
  - Schema validation for capability input fields
  - SerDe when reading/writing capability data from/to the database

Nodes use their own copy in nodes/common/capability.py which may have
additional node-specific fields. The two are intentionally separate: nodes
own the capability definition, the server only mediates and routes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityInputField:
    """A single input field of a capability schema."""

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    ge: float | None = None
    le: float | None = None
    description: str = ""

    def validate(self, value: Any) -> list[str]:
        """Validate a value against this field. Returns a list of errors."""
        errors: list[str] = []
        if value is None:
            if self.required and self.default is None:
                errors.append(f"Field '{self.name}' is required.")
            return errors
        if self.enum is not None and value not in self.enum:
            errors.append(
                f"Field '{self.name}': value {value!r} not in {self.enum!r}."
            )
        if self.ge is not None or self.le is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(
                    f"Field '{self.name}': value {value!r} must be numeric."
                )
            else:
                if self.ge is not None and value < self.ge:
                    errors.append(
                        f"Field '{self.name}': value {value!r} must >= {self.ge}."
                    )
                if self.le is not None and value > self.le:
                    errors.append(
                        f"Field '{self.name}': value {value!r} must <= {self.le}."
                    )
        return errors


@dataclass
class CapabilityInputSchema:
    """Schema over all input fields of a capability with payload validation."""

    fields: dict[str, CapabilityInputField] = field(default_factory=dict)

    @staticmethod
    def _iter_raw_fields(raw_fields):
        if isinstance(raw_fields, dict):
            yield from raw_fields.items()
        else:
            yield from ((f"field_{i}", r) for i, r in enumerate(raw_fields))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityInputSchema:
        """Build a schema from a dictionary (e.g. from capabilities.yaml)."""
        if not data:
            return cls()
        raw_fields = data.get("fields", data) if isinstance(data, dict) else {}
        fields: dict[str, CapabilityInputField] = {}
        for key, raw in cls._iter_raw_fields(raw_fields):
            name = raw.get("name") or key
            fld = CapabilityInputField(
                name=name,
                type=raw.get("type", "string"),
                required=raw.get("required", False),
                default=raw.get("default"),
                enum=raw.get("enum"),
                ge=raw.get("ge"),
                le=raw.get("le"),
                description=raw.get("description", ""),
            )
            fields[fld.name] = fld
        return cls(fields=fields)

    def validate_payload(self, payload: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate a payload against this schema.

        Checks:
        - All fields marked `required` are present.
        - Fields without a value receive their `default`.
        - Enum constraints are enforced.
        - Numeric bounds (ge/le) are checked.
        - Unknown fields are reported as errors.

        Returns:
            Tuple (is_valid, error_messages).
        """
        errors: list[str] = []
        if not isinstance(payload, dict):
            return False, ["Payload must be a dictionary."]
        for key in payload:
            if key not in self.fields:
                errors.append(f"Unexpected field '{key}' in payload.")
        for name, fld in self.fields.items():
            if name not in payload or payload[name] is None:
                if fld.required and fld.default is None:
                    errors.append(f"Field '{name}' is required.")
                    continue
                value = fld.default
            else:
                value = payload[name]
            if value is None:
                continue
            errors.extend(fld.validate(value))
        return (len(errors) == 0), errors
