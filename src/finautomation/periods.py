from calendar import monthrange
from datetime import date


def month_index(day: date):
    return day.year * 12 + day.month - 1


def shift_months(day: date, count: int, *, preserve_month_end=False):
    year, month = divmod(month_index(day) + count, 12)
    month += 1
    last_day = monthrange(year, month)[1]
    if preserve_month_end and day.day == monthrange(day.year, day.month)[1]:
        return date(year, month, last_day)
    return date(year, month, min(day.day, last_day))
