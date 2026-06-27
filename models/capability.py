from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityInputField:
    """Ein einzelnes Eingabefeld eines Capability-Schemas."""

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    ge: float | None = None
    le: float | None = None
    description: str = ""

    def validate(self, value: Any) -> list[str]:
        """Prüft einen Wert gegen dieses Feld. Liefert eine Liste von Fehlern."""
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
    """Schema über alle Eingabefelder einer Capability mit Payload-Validierung."""

    fields: dict[str, CapabilityInputField] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityInputSchema:
        """Erzeugt ein Schema aus einem Dictionary (z. B. aus capabilities.yaml)."""
        if not data:
            return cls()
        raw_fields = data.get("fields", data) if isinstance(data, dict) else {}
        fields: dict[str, CapabilityInputField] = {}
        for raw in raw_fields.values() if isinstance(raw_fields, dict) else raw_fields:
            fld = CapabilityInputField(
                name=raw["name"],
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
        Validiert eine Payload gegen dieses Schema.

        Prüft:
        - Alle als `required` markierten Felder sind vorhanden.
        - Felder ohne Wert erhalten ihren `default`.
        - Enum-Constraints werden erzwungen.
        - Numerische Grenzen (ge/le) werden geprüft.
        - Unbekannte Felder werden als Fehler gemeldet.

        Returns:
            Tupel (is_valid, error_messages).
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
