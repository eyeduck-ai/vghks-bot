"""Opt-in list pagination; omitted parameters preserve legacy responses."""


def page(values):
    if not any(key in values for key in ("limit", "offset", "page")):
        return None, 0
    try:
        limit = min(200, max(1, int(values.get("limit", 40))))
        offset = max(0, int(values.get("offset", 0)))
        if "page" in values:
            offset = max(0, int(values["page"]) - 1) * limit
    except (TypeError, ValueError):
        raise ValueError("分頁參數不正確。") from None
    return limit, offset


def slice_rows(rows, values):
    limit, offset = page(values)
    return rows if limit is None else rows[offset:offset + limit]
