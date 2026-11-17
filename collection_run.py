"""Run boundaries and immutable, successful-observation snapshots for Actions."""
import argparse
import csv
import json
import os
import shutil
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
KEY = ("supplier", "sku")


def run_slot(schedule, created, now, period=None, target_date=None):
    created, now = created.astimezone(KYIV), now.astimezone(KYIV)
    mode = {"30 9 * * *": "morning", "0 17 * * *": "evening"}.get(schedule)
    if schedule and mode is None:
        raise ValueError("Unknown schedule")
    if not schedule and (period not in ("morning", "evening") or not target_date):
        raise ValueError("Dispatch requires an explicit period and target date")
    mode = mode or period
    day = target_date or created.date().isoformat()
    if day != datetime.fromisoformat(day).date().isoformat():
        raise ValueError("Target date must be YYYY-MM-DD")
    # Reserve the workflow's 25-minute timeout before the next slot/day.
    start, end = (time(9, 30), time(16, 35)) if mode == "morning" else (time(17), time(23, 35))
    allowed = created.date() == now.date() and day == now.date().isoformat() and start <= now.time() < end
    return mode, allowed


def read_rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def prepare(root, work, schedule, created, now, period=None, target_date=None, permit=True):
    mode, allowed = run_slot(schedule, created, now, period, target_date)
    allowed = allowed and permit
    meta = {"date": target_date or created.astimezone(KYIV).date().isoformat(), "mode": mode,
            "started_at": now.isoformat(), "ref": os.environ.get("GITHUB_REF", "local")}
    if allowed:
        backup = work / "backup"
        backup.mkdir(parents=True, exist_ok=False)
        for name in ("price_history.csv", "collection_status.csv"):
            source = root / "data" / name
            if source.exists():
                shutil.copyfile(source, backup / name)
        (backup / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta, allowed


def capture(root, work, now):
    from app import load_history
    meta = json.loads((work / "backup/run.json").read_text(encoding="utf-8"))
    started = datetime.fromisoformat(meta["started_at"])
    day = meta["date"]
    end = now.astimezone(KYIV)
    if end.date().isoformat() != day or (meta["mode"] == "morning" and end.hour >= 17):
        raise ValueError("Collection crossed its reporting boundary; do not persist")
    path = root / "data/price_history.csv"
    if path.exists():
        load_history(path)  # Existing strict price, date, SKU and promo validation.
    rows = read_rows(path)
    before = read_rows(work / "backup/price_history.csv")
    # Never publish a collection that changed any past/future day's rows.
    if [r for r in rows if r["date"] != day] != [r for r in before if r["date"] != day]:
        raise ValueError("Collection changed history outside the current day")
    statuses = read_rows(root / "data/collection_status.csv")
    keys = [tuple(r[k] for k in KEY) for r in statuses]
    if len(keys) != len(set(keys)) or not statuses:
        raise ValueError("Missing or duplicate collection statuses")
    for row in statuses:
        stamp = datetime.fromisoformat(row["checked_at"])
        if stamp.tzinfo is None or not started <= stamp <= now or stamp.astimezone(KYIV).date().isoformat() != day:
            raise ValueError("Status does not belong to this collection")
    successful = {tuple(r[k] for k in KEY) for r in statuses if r["status"] == "ok"}
    current = [r for r in rows if r["date"] == day and tuple(r[k] for k in KEY) in successful]
    if len(current) != len(successful):
        raise ValueError("Successful status without a unique current-day price")
    snapshot = dict(meta, finished_at=now.isoformat(), rows=current, statuses=statuses)
    folder = work / "current"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return snapshot


def comparison(history, current, morning=None):
    """Morning uses Dashboard rules; evening joins real successful snapshots."""
    import pandas as pd
    from app import daily_changes
    columns = ["supplier", "sku", "product", "price", "regular_price", "discount_pct", "is_promo"]

    def frame(snapshot):
        data = pd.DataFrame(snapshot["rows"], columns=columns)
        for name in ("price", "regular_price", "discount_pct"):
            data[name] = pd.to_numeric(data[name], errors="coerce")
        data["is_promo"] = data["is_promo"].map({"true": True, "false": False}).astype("boolean")
        return data

    actual = frame(current)
    promo = actual.loc[actual["is_promo"].eq(True).fillna(False)]
    lines = [f"Подтверждённых акций в этом сборе: {len(promo)}"]
    if current["mode"] == "morning":
        lines.extend(f"Акция: {r.product} · {r.supplier}: {r.price:.2f} грн ({r.discount_pct:.2f}%)"
                     for r in promo.itertuples())
        if history.empty:
            changes, count = pd.DataFrame(columns=[*columns, "change", "change_pct"]), 0
        else:
            day = datetime.fromisoformat(current["date"]).date()
            keys = set(zip(actual.supplier, actual.sku))
            keep = history["date"].dt.date.ne(day) | pd.Series(
                [key in keys for key in zip(history.supplier, history.sku)], index=history.index)
            changes, count = daily_changes(history.loc[keep], day)
        return changes, count, lines
    try:
        finished = datetime.fromisoformat(morning["finished_at"]).astimezone(KYIV)
        valid = (morning.get("mode") == "morning" and morning.get("date") == current["date"]
                 and morning.get("ref") == current["ref"] and finished.date().isoformat() == current["date"]
                 and finished.hour < 17 and finished < datetime.fromisoformat(current["started_at"]))
    except (KeyError, TypeError, ValueError):
        valid = False
    baseline = frame(morning) if valid else frame({"rows": []})
    paired = actual.merge(baseline.drop(columns="product"), on=list(KEY), suffixes=("", "_morning"), validate="one_to_one")
    paired = paired.rename(columns={"price_morning": "previous_price"})
    changed = paired.loc[paired.price.ne(paired.previous_price)].copy()
    changed["change"] = changed.price - changed.previous_price
    changed["change_pct"] = changed.change / changed.previous_price * 100
    lines.append(f"Без успешной утренней пары: {len(actual) - len(paired)}")
    if not valid:
        lines.append("Утренний снимок отсутствует: внутридневное сравнение недоступно.")
    for row in paired.to_dict("records"):
        old, new = row["is_promo_morning"], row["is_promo"]
        if pd.isna(old) or pd.isna(new):
            continue  # Unknown is never evidence that a promotion ended.
        changed_terms = any(not (pd.isna(row[k]) and pd.isna(row[k + "_morning"]))
                            and row[k] != row[k + "_morning"] for k in ("regular_price", "discount_pct"))
        if old != new or (new and (changed_terms or row["price"] != row["previous_price"])):
            label = "началась" if new and not old else "завершилась" if old and not new else "условия изменились"
            lines.append(f"Акция {label}: {row['product']} · {row['supplier']}")
    return changed, len(paired), lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture"))
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    now = datetime.now(KYIV)
    if args.action == "prepare":
        created = datetime.fromisoformat(os.environ["RUN_CREATED_AT"].replace("Z", "+00:00"))
        schedule = os.environ.get("RUN_SCHEDULE", "")
        period, day = os.environ.get("REQUESTED_PERIOD"), os.environ.get("TARGET_DATE")
        mode, allowed = run_slot(schedule, created, now, period, day)
        permit = True
        if allowed and os.environ.get("SPM_GUARD") == "true":
            from scheduler_guard import GitHub, may_collect
            permit = may_collect(GitHub(), day or created.astimezone(KYIV).date().isoformat(), mode,
                                 os.environ.get("RESERVE_REQUEST", ""))
        meta, allowed = prepare(root, args.work, schedule, created, now, period, day, permit)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"run={str(allowed).lower()}\nmode={meta['mode']}\ndate={meta['date']}\n")
        if not allowed:
            print("Skipped: run is outside its Kyiv date/collection slot.")
    else:
        capture(root, args.work, now)


if __name__ == "__main__":
    main()
