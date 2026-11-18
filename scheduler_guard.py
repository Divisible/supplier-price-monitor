"""Fail-closed scheduling guard. Only GitHub metadata is written, never price CSVs."""
import argparse
import json
import os
import re
from datetime import datetime, timedelta, time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen

from collection_run import KYIV, run_slot

WORKFLOW = "price-report.yml"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


class GitHubError(RuntimeError):
    pass


class GitHub:
    def request(self, path, method="GET", body=None, missing=False, conflict=False, pagination=False):
        url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}"
        headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                   "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            request = Request(url, headers=headers, method=method,
                              data=json.dumps(body).encode() if body is not None else None)
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                data = json.loads(raw) if raw else {}
                return (data, response.headers.get("Link", "")) if pagination else data
        except HTTPError as exc:
            if missing and exc.code == 404:
                return None
            if conflict and exc.code == 422:
                return None  # Existing ref OR validation failure: neither authorizes dispatch.
            raise GitHubError(f"GitHub API HTTP {exc.code}; no automatic retry") from None
        except Exception:
            raise GitHubError("GitHub API unavailable or invalid response; no automatic retry") from None

    def listing(self, path, field, **params):
        rows = []
        seen = set()
        expected = None
        for page in range(1, 11):
            query = dict(params, per_page=100, page=page)
            # Log only known query fields, never headers, response bodies or Link URLs.
            safe = {k: v for k, v in query.items() if k in ("branch", "status", "created", "filter", "per_page", "page")}
            context = f"endpoint={path.split('?')[0]} params={urlencode(safe)}"

            def fail(reason):
                raise GitHubError(f"GitHub pagination: {context}; {reason}") from None

            try:
                data, link = self.request(path + "?" + urlencode(query), pagination=True)
            except GitHubError as exc:
                code = re.search(r"HTTP \d{3}", str(exc))
                fail(code[0] if code else "API unavailable or invalid JSON; no automatic retry")
            if not isinstance(data, dict) or not isinstance(data.get(field), list):
                fail("missing or invalid result list")
            total = data.get("total_count")
            if type(total) is not int or total < 0:
                fail("missing or invalid total_count")
            if total >= 1000:
                fail("total_count reaches API safety limit (1000)")
            if expected is not None and total != expected:
                fail(f"total_count changed: expected={expected} observed={total}")
            expected = total
            batch = data[field]
            if len(batch) > 100:
                fail("response exceeds per_page")
            for row in batch:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in seen:
                    fail("missing, invalid or duplicate record id")
                seen.add(row["id"])
            rows.extend(batch)
            next_pages = []
            for part in link.split(",") if link else []:
                match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
                if not match:
                    fail("invalid pagination Link")
                if match[2] == "next":
                    try:
                        next_pages.append(parse_qs(urlsplit(match[1]).query).get("page"))
                    except ValueError:
                        fail("invalid next page URL")
            if next_pages and next_pages != [[str(page + 1)]]:
                fail("invalid next page")
            if not next_pages:
                if len(rows) != total:
                    fail(f"terminal total_count mismatch: expected={total} received={len(rows)}")
                return rows
            if not batch or len(rows) >= total:
                fail(f"next page contradicts response: expected={total} received={len(rows)}")
        fail("page limit exceeded")

    def marker(self, kind, day, period):
        ref = self.request(f"git/ref/tags/spm-{kind}/{day}-{period}", missing=True)
        if ref is None:
            return None
        if ref["object"]["type"] != "tag":
            raise GitHubError("Invalid scheduler marker")
        tag = self.request(f"git/tags/{ref['object']['sha']}")
        data = json.loads(tag["message"])
        if data.get("date") != day or data.get("period") != period or data.get("kind") != kind:
            raise GitHubError("Mismatched scheduler marker")
        return data

    def claim(self, kind, day, period, request_id):
        name = f"spm-{kind}/{day}-{period}"
        data = dict(kind=kind, date=day, period=period, request_id=request_id,
                    run_id=os.environ["GITHUB_RUN_ID"], attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
        tag = self.request("git/tags", "POST", dict(tag=name, message=json.dumps(data),
                           object=os.environ["GITHUB_SHA"], type="commit"))
        # Create-only ref is the atomic claim. Never update/delete it, even after timeout.
        return self.request("git/refs", "POST", dict(ref="refs/tags/" + name, sha=tag["sha"]), conflict=True) is not None


def outcome(steps):
    """Only a real collection plus validated persistence can be completed."""
    by_name = {s["name"]: s.get("conclusion") for s in steps}
    if by_name.get("Confirm successful collection") == "success":
        required = ("Collect prices", "Validate history boundaries and capture this collection",
                    "Persist validated observations and diagnostics")
        if all(by_name.get(name) == "success" for name in required):
            return "completed"
        raise GitHubError("Inconsistent collection completion evidence")
    if by_name.get("Check Kyiv slot and prepare rollback copy") == "success" and by_name.get("Collect prices") == "skipped":
        return "skipped"
    return "failed"


def completed(api, day, period):
    marker = api.marker("completed", day, period)
    if marker is not None:
        # Marker is written after successful persistence. Do not depend on notification jobs.
        return True
    return False


def inspect_runs(api, day, period, exclude=None, active=True):
    path = f"actions/workflows/{WORKFLOW}/runs"
    if active:
        for status in ACTIVE:
            if any(str(r["id"]) != str(exclude) for r in api.listing(path, "workflow_runs", branch="main", status=status)):
                return "active"
    start = datetime.fromisoformat(day).replace(tzinfo=KYIV)
    # UTC endpoints include both Kyiv date boundaries across DST.
    from datetime import timezone
    dates = f"{start.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}..{(start + timedelta(days=1)).astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    runs = api.listing(path, "workflow_runs", branch="main", created=dates)
    states = []
    for run in runs:
        if str(run["id"]) == str(exclude):
            continue
        match = re.fullmatch(r"SPM collect \| (morning|evening) \| (scheduled|\d{4}-\d{2}-\d{2})", run["display_title"])
        if match:
            target = match[2] if match[2] != "scheduled" else datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(KYIV).date().isoformat()
            if (target, match[1]) != (day, period):
                continue
        if run["status"] != "completed":
            if active:
                return "active"
            continue  # Same collection concurrency group: these cannot be writing now.
        jobs = api.listing(f"actions/runs/{run['id']}/jobs", "jobs", filter="all")
        collectors = [j for j in jobs if j["name"] == "collect"]
        if not collectors:
            raise GitHubError("Collection job evidence unavailable")
        for job in collectors:
            steps = job["steps"]
            by_name = {s["name"]: s for s in steps}
            attempt = by_name.get("Collect prices")
            if attempt is None:
                raise GitHubError("Collection step evidence unavailable")
            if match or attempt.get("conclusion") == "skipped":
                states.append(outcome(steps))
                continue
            # Migration: old runs had no period in their title. Only an executed
            # step supplies a real collection timestamp, never workflow success alone.
            stamp = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00")).astimezone(KYIV)
            if (stamp.date().isoformat(), "morning" if stamp.hour < 17 else "evening") != (day, period):
                continue
            required = ["Collect prices", "Validate history boundaries and capture this collection",
                        "Persist validated observations and diagnostics"]
            if period == "morning":
                required.append("Preserve immutable morning snapshot")
            persisted = all(by_name.get(name, {}).get("conclusion") == "success" for name in required)
            if persisted and job.get("conclusion") == "success":
                states.append("completed")
            elif persisted and by_name.get("Flag collection without successful observations", {}).get("conclusion") != "failure":
                raise GitHubError("Legacy collection persisted but completion is uncertain")
            else:
                states.append("failed")
    return "completed" if "completed" in states else "failed" if "failed" in states else "skipped" if states else "missing"


def may_collect(api, day, period, reserve_request):
    if completed(api, day, period) or inspect_runs(api, day, period, os.environ["GITHUB_RUN_ID"], active=False) == "completed":
        return False
    if reserve_request:
        claim = api.marker("reserve", day, period)
        if claim is None or claim["request_id"] != reserve_request:
            raise GitHubError("Reserve dispatch has no matching claim")
        return api.claim("reserve-started", day, period, reserve_request)
    return True


def check(api, period, created, now, request_id):
    day = created.astimezone(KYIV).date().isoformat()
    _, allowed = run_slot("", created, now, period, day)
    threshold = time(9, 50) if period == "morning" else time(17, 20)
    if not allowed or now.astimezone(KYIV).time() < threshold:
        return "skipped: outside check window"
    if completed(api, day, period):
        return "completed"
    state = inspect_runs(api, day, period)
    if state in ("completed", "active"):
        return state
    if api.marker("reserve", day, period) is not None:
        return "reserve already requested"
    if not api.claim("reserve", day, period, request_id):
        return "reserve not claimed"
    # A main run may have appeared during the claim. Leave the claim in place on uncertainty.
    if completed(api, day, period) or inspect_runs(api, day, period) in ("completed", "active"):
        return "reserve suppressed after recheck"
    api.request(f"actions/workflows/{WORKFLOW}/dispatches", "POST", {
        "ref": "main", "inputs": {"period": period, "target_date": day, "reserve_request": request_id}})
    return "reserve requested"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "complete"))
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise GitHubError("Scheduler writes are restricted to main")
    api = GitHub()
    request_id = f"{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    if args.action == "complete":
        meta = json.loads((args.work / "backup/run.json").read_text(encoding="utf-8"))
        if not api.claim("completed", meta["date"], meta["mode"], request_id):
            raise GitHubError("Completion marker could not be created")
        result = "completed"
    else:
        run = api.request(f"actions/runs/{os.environ['GITHUB_RUN_ID']}")
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        result = check(api, os.environ["REQUESTED_PERIOD"], created, datetime.now(KYIV), request_id)
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"SPM scheduler: {result}\n")


if __name__ == "__main__":
    try:
        main()
    except GitHubError as exc:
        raise SystemExit(f"SPM scheduler failed closed: {exc}") from None
    except Exception:
        raise SystemExit("SPM scheduler failed closed; inspect API availability and run state. No automatic retry.") from None
