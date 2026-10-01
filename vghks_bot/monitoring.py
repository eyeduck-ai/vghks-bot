"""Shared, bounded monitor interval validation."""


def interval(values, current):
    if "interval_value" in values or "interval_unit" in values:
        value, unit = values.get("interval_value"), values.get("interval_unit")
        if type(value) is not int or unit not in {"hours", "days"}:
            raise ValueError("請輸入整數頻率，單位為小時或天。")
        hours = value * (24 if unit == "days" else 1)
    else:
        hours = values.get("hours", current.get("hours", 24))
        value, unit = (hours // 24, "days") if type(hours) is int and hours % 24 == 0 else (hours, "hours")
    if type(hours) is not int or not 1 <= hours <= 720:
        raise ValueError("監控頻率需介於一小時至三十天。")
    return {"hours": hours, "interval_value": value, "interval_unit": unit}
