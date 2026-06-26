"""
Capability-Input-Schemas – definieren, wie ein Task-Aufruf
für eine bestimmte Capability aussehen muss.

Ein Agent/Client liest das Input-Schema via Discovery-API,
validiert sein Payload lokal und schickt dann den Task ab.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field


class CapabilityInputField(BaseModel):
    """Ein einzelnes Feld im Input-Schema einer Capability."""

    type: Literal["string", "integer", "float", "boolean", "enum", "file", "list"]
    required: bool = False
    default: Optional[Any] = None
    description: str = ""

    # ── Constraints (je nach Typ befüllt) ──────────────────────
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None          # Regex (für string)
    ge: Optional[float] = None             # ≥ (integer/float)
    le: Optional[float] = None             # ≤ (integer/float)
    enum_values: Optional[list[Any]] = None  # erlaubte Werte (enum)
    items: Optional[CapabilityInputField] = None  # Element-Typ (für list)


class CapabilityInputSchema(BaseModel):
    """
    Gesamtes Input-Schema einer Capability.

    Beispiel:
        CapabilityInputSchema(fields={
            "prompt":   CapabilityInputField(type="string", required=True),
            "steps":    CapabilityInputField(type="integer", default=20, ge=1, le=50),
            "format":   CapabilityInputField(type="enum", enum_values=["png","jpg","webp"])
        })
    """

    fields: dict[str, CapabilityInputField] = Field(default_factory=dict)

    def validate_payload(self, payload: dict[str, Any]) -> list[str]:
        """Prüft ein Payload-Dict gegen dieses Schema. Gibt Fehlerliste zurück."""
        errors: list[str] = []

        for field_name, field_def in self.fields.items():
            value: Any = payload.get(field_name)

            # Pflichtfeld fehlt?
            if field_def.required and value is None:
                errors.append(f"'{field_name}' ist erforderlich")
                continue

            # Optionales Feld nicht angegeben → ok
            if value is None:
                continue

            # Typprüfung + Constraints
            errs = self._validate_field(field_def, value, field_name)
            errors.extend(errs)

        return errors

    @staticmethod
    def _validate_field(field: CapabilityInputField, value: Any, path: str) -> list[str]:
        """Prüft einen einzelnen Wert gegen sein Feld-Definition."""
        errors: list[str] = []
        t = field.type

        # ── Typprüfung ──
        if t in ("string", "enum", "file"):
            if not isinstance(value, str):
                errors.append(f"'{path}' muss ein String sein, ist {type(value).__name__}")
                return errors
        elif t in ("integer",):
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"'{path}' muss ein Integer sein, ist {type(value).__name__}")
                return errors
        elif t == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"'{path}' muss eine Zahl sein, ist {type(value).__name__}")
                return errors
        elif t == "boolean":
            if not isinstance(value, bool):
                errors.append(f"'{path}' muss ein Boolean sein, ist {type(value).__name__}")
                return errors
        elif t == "file":
            if not isinstance(value, str):
                errors.append(f"'{path}' muss ein String (Dateipfad/URL) sein")
                return errors
        elif t == "list":
            if not isinstance(value, (list, tuple)):
                errors.append(f"'{path}' muss eine Liste sein, ist {type(value).__name__}")
                return errors
            # Elemente validieren, falls items definiert
            if field.items and isinstance(value, Sequence):
                for i, item in enumerate(value):
                    sub = CapabilityInputSchema._validate_field(
                        field.items, item, f"{path}[{i}]"
                    )
                    errors.extend(sub)
            return errors

        # ── Range-Constraints (nur für Zahlen) ──
        if t in ("integer", "float") and isinstance(value, (int, float)):
            if field.ge is not None and value < field.ge:
                errors.append(f"'{path}' muss ≥ {field.ge} sein, ist {value}")
            if field.le is not None and value > field.le:
                errors.append(f"'{path}' muss ≤ {field.le} sein, ist {value}")

        # ── String-Constraints ──
        if t in ("string", "enum") and isinstance(value, str):
            if field.min_length is not None and len(value) < field.min_length:
                errors.append(f"'{path}' muss mindestens {field.min_length} Zeichen haben")
            if field.max_length is not None and len(value) > field.max_length:
                errors.append(f"'{path}' darf höchstens {field.max_length} Zeichen haben")
            if field.pattern is not None:
                import re
                if not re.match(field.pattern, value):
                    errors.append(f"'{path}' passt nicht zum Muster: {field.pattern}")

        # ── Enum-Constraint ──
        if t == "enum" and isinstance(value, str) and field.enum_values is not None:
            if value not in field.enum_values:
                errors.append(
                    f"'{path}' muss eins von {field.enum_values} sein, ist '{value}'"
                )

        return errors