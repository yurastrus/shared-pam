"""Optional local recording-date scope shared by PAM workflow links."""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RecordingScope:
    start: date
    end: date
    months: tuple[int, ...]

    @classmethod
    def from_params(cls, params):
        start = params.get("recording_from")
        end = params.get("recording_to")
        months = params.get("recording_months")
        if start is None and end is None and months is None:
            return None
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
            selected = tuple(sorted({int(value) for value in months.split(",")})) if months else ()
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Invalid recording date scope") from exc
        if start_date > end_date or any(not 1 <= month <= 12 for month in selected):
            raise ValueError("Invalid recording date scope")
        return cls(start_date, end_date, selected)

    def where(self, alias):
        local = f"({alias}.datetime_start AT TIME ZONE 'Europe/Kyiv')"
        conditions = [f"{local}::date BETWEEN :recording_from AND :recording_to"]
        values = {"recording_from": self.start, "recording_to": self.end}
        if self.months:
            conditions.append(f"EXTRACT(MONTH FROM {local}) = ANY(:recording_months)")
            values["recording_months"] = list(self.months)
        return conditions, values
